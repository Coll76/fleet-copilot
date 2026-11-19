"""Tool registry and dispatcher.

Turns pydantic argument models into Anthropic tool definitions, validates every
call the model makes, records evidence and numbers, and converts failures into
is_error tool results so the model can correct itself instead of crashing the run.
"""
from __future__ import annotations

import json
import time
from typing import Any

from pydantic import ValidationError

from .action_tools import ACTION_TOOLS
from .context import ToolContext
from .read_tools import READ_TOOLS, ToolError

ALL_TOOLS = {**READ_TOOLS, **ACTION_TOOLS}


def tool_specs(include_actions: bool = True) -> list[dict[str, Any]]:
    names = list(READ_TOOLS) + (list(ACTION_TOOLS) if include_actions else [])
    specs = []
    for name in names:
        _, model, desc = ALL_TOOLS[name]
        schema = model.model_json_schema()
        schema.pop("title", None)
        specs.append({"name": name, "description": desc, "input_schema": schema})
    return specs


def _collect_numbers(obj: Any, out: set[str]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        for v in {obj, round(obj), round(float(obj), 1)}:
            out.add(_norm(v))
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)
    elif isinstance(obj, str):
        from ..agent.verifier import NUM_RE, strip_ids
        for m in NUM_RE.findall(strip_ids(obj)):
            out.add(_norm(float(m)))


def _norm(v: float) -> str:
    f = float(v)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


def dispatch(ctx: ToolContext, name: str, raw_args: dict[str, Any]) -> tuple[str, bool]:
    """Run one tool call. Returns (json_text, is_error)."""
    t0 = time.perf_counter()
    entry: dict[str, Any] = {"tool": name, "args": raw_args}
    try:
        if name not in ALL_TOOLS:
            raise ToolError(f"Unknown tool {name!r}.")
        if name in ACTION_TOOLS and ctx.session.role != "it_admin":
            raise ToolError(f"Role {ctx.session.role!r} cannot propose actions.")
        fn, model, _ = ALL_TOOLS[name]
        args = model.model_validate(raw_args or {})
        result = fn(ctx, args)
        _collect_numbers(result, ctx.numbers)
        text, err = json.dumps(result, default=str), False
        entry["status"] = result.get("status", "ok") if isinstance(result, dict) else "ok"
    except ValidationError as e:
        msgs = "; ".join(f"{'.'.join(map(str, x['loc'])) or 'args'}: {x['msg']}" for x in e.errors())
        text, err = json.dumps({"error": f"Invalid arguments. {msgs}"}), True
        entry["status"] = "invalid_args"
    except ToolError as e:
        text, err = json.dumps({"error": str(e)}), True
        entry["status"] = "error"
    entry["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    entry["is_error"] = err
    ctx.tool_log.append(entry)
    return text, err
