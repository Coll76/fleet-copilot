"""Thin Claude adapter with record and replay.

Why not LangChain's chat-model wrapper: LangGraph owns control flow here, but each
model call goes straight through the official Anthropic SDK. That keeps tool
schemas, thinking blocks, prompt caching and refusal fallback exactly as the API
defines them, and makes record/replay trivial.

Determinism: the current Claude API does not accept a temperature parameter, so
"temperature 0" is not available. Reproducibility comes from three things instead:
  1. All arithmetic, filtering and trend detection happens in deterministic tools.
  2. Every model call can be recorded to a cassette keyed by a hash of the full
     request (model, system prompt, tools, messages, output schema).
  3. Replay mode serves those cassettes, so the evaluation suite reruns byte-for-byte
     without an API key. A request that was never recorded fails loudly.

Modes (FLEET_LLM_MODE): live | record | replay | auto (replay if recorded, else live and record).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.environ.get("FLEET_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("FLEET_EFFORT", "medium")
CASSETTE_DIR = Path(os.environ.get("FLEET_CASSETTES", Path(__file__).resolve().parents[3] / "evals" / "cassettes"))

# USD per million tokens (input, output, cache read). Used only for reporting.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-haiku-5-5": (0.10, 0.50, 0.01)}


class CassetteMiss(RuntimeError):
    pass


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    replayed_calls: int = 0
    latency_s: float = 0.0
    models: dict[str, int] = field(default_factory=dict)

    def add(self, model: str, u: dict[str, Any], latency: float, replayed: bool) -> None:
        self.calls += 1
        self.replayed_calls += int(replayed)
        self.input_tokens += u.get("input_tokens", 0) or 0
        self.output_tokens += u.get("output_tokens", 0) or 0
        self.cache_read_tokens += u.get("cache_read_input_tokens", 0) or 0
        self.latency_s += latency
        self.models[model] = self.models.get(model, 0) + 1

    def cost_usd(self) -> float:
        total = 0.0
        for m in self.models:
            pi, po, pc = PRICES.get(m, PRICES["claude-opus-5-5"])
            total += (self.input_tokens * pi + self.output_tokens * po + self.cache_read_tokens * pc) / 1e6
        return round(total, 4)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


class LLM:
    def __init__(self, model: str = DEFAULT_MODEL, mode: str | None = None, effort: str = DEFAULT_EFFORT,
                 cassette_dir: Path | str = CASSETTE_DIR, fallbacks: bool = True):
        self.model = model
        self.mode = mode or os.environ.get("FLEET_LLM_MODE", "auto")
        self.effort = effort
        self.cassette_dir = Path(cassette_dir)
        self.fallbacks = fallbacks
        self.usage = Usage()
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def _key(self, req: dict[str, Any]) -> str:
        return hashlib.sha256(_canonical(req).encode()).hexdigest()[:24]

    def call(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
             output_schema: dict[str, Any] | None = None, max_tokens: int = 16000) -> dict[str, Any]:
        req: dict[str, Any] = {"model": self.model, "system": system, "messages": messages,
                               "tools": tools or [], "output_schema": output_schema, "effort": self.effort}
        key = self._key(req)
        path = self.cassette_dir / f"{key}.json"
        if self.mode in ("replay", "auto") and path.exists():
            rec = json.loads(path.read_text())
            self.usage.add(rec["response"].get("model", self.model), rec["response"].get("usage", {}), 0.0, True)
            return rec["response"]
        if self.mode == "replay":
            raise CassetteMiss(f"No recorded response for request {key}. Run with FLEET_LLM_MODE=record "
                               "(needs ANTHROPIC_API_KEY) to record it.")
        t0 = time.perf_counter()
        resp = self._live(system, messages, tools, output_schema, max_tokens)
        dt = time.perf_counter() - t0
        self.usage.add(resp.get("model", self.model), resp.get("usage", {}), dt, False)
        if self.mode in ("record", "auto"):
            self.cassette_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"key": key, "request_summary": {
                "model": self.model, "last_message": _canonical(messages[-1])[:300]}, "response": resp}, indent=1))
        return resp

    def _live(self, system, messages, tools, output_schema, max_tokens) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self.effort}
        if output_schema:
            output_config["format"] = {"type": "json_schema", "schema": output_schema}
        kwargs: dict[str, Any] = dict(
            model=self.model, max_tokens=max_tokens,
            # The system prompt and tool list are identical on every call, so they are cached.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages, thinking={"type": "adaptive"}, output_config=output_config,
        )
        if tools:
            kwargs["tools"] = tools
        if self.fallbacks:
            # If a safety classifier declines, the API reruns the request on a fallback model in the same call.
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        resp = self.client.beta.messages.create(**kwargs)
        return resp.model_dump(mode="json", exclude_none=True)


def text_of(resp: dict[str, Any]) -> str:
    return "\n".join(b["text"] for b in resp.get("content", []) if b.get("type") == "text").strip()


def tool_uses(resp: dict[str, Any]) -> list[dict[str, Any]]:
    return [b for b in resp.get("content", []) if b.get("type") == "tool_use"]
