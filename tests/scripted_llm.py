"""A stand-in for the LLM that returns scripted responses in order.

Used by the unit tests to drive the real graph, tools, policy, verifier and audit
log without network access. It also records every request it receives so tests
can assert on what the model was shown (for example, which tools were offered).
"""
from __future__ import annotations

import json
from typing import Any

from fleet_copilot.agent.llm import Usage


def plan(intent="question", actions=False, steps=None, oos=""):
    return {"content": [{"type": "text", "text": json.dumps({
        "intent": intent, "restated_question": "q", "assumptions": [], "steps": steps or [],
        "may_propose_actions": actions, "out_of_scope_reason": oos})}], "stop_reason": "end_turn", "usage": {}}


def call(*tools: tuple[str, dict[str, Any]]):
    return {"content": [{"type": "tool_use", "id": f"tu_{i}_{name}", "name": name, "input": args}
                        for i, (name, args) in enumerate(tools)], "stop_reason": "tool_use", "usage": {}}


def say(text: str):
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "usage": {}}


class ScriptedLLM:
    def __init__(self, script: list[dict[str, Any]]):
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.usage = Usage()
        self.model = "scripted"

    def call(self, **req) -> dict[str, Any]:
        self.requests.append(req)
        if not self.script:
            raise AssertionError("ScriptedLLM ran out of responses")
        return self.script.pop(0)
