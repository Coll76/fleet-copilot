"""Fleet Copilot agent graph (LangGraph).

    START
      |
  input_guard --refuse--> finalize
      |
    plan ------out_of_scope--> finalize
      |
    agent <--------------+-----------+
      |  tool_use         |           |
      +-----> tools ------+           |
      |  final text                   |
    verify --fail, 1 repair left------+
      |
      +-- proposals pending --> approval (interrupt: human decides) --> execute
      |                                                                   |
    finalize <------------------------------------------------------------+
      |
     END

Every node writes to the audit log. The ToolContext (tenant view, ledger, proposals)
is rebuilt from graph state in each node, so a paused turn can resume after the
human decides, in another process if the checkpointer is persistent.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..analytics.policy import Policy
from ..data.store import TelemetryStore
from ..guardrails.audit import AuditLog
from ..tools.action_tools import ACTION_TOOLS, recheck
from ..tools.context import Session, ToolContext
from ..tools.registry import dispatch, tool_specs
from .llm import LLM, text_of, tool_uses
from .prompts import AGENT_SYSTEM, PLAN_SCHEMA, PLANNER_SYSTEM, REPAIR_TEMPLATE
from .verifier import verify

MAX_TOOL_ROUNDS = 8
MAX_REPAIRS = 1
ROOT = Path(__file__).resolve().parents[3]


class TurnState(TypedDict, total=False):
    tenant_id: str
    user_id: str
    role: str
    question: str
    history: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    plan: dict[str, Any]
    rounds: int
    ledger: dict[str, Any]
    numbers: list[str]
    proposals: list[dict[str, Any]]
    tool_log: list[dict[str, Any]]
    draft: str
    verification: dict[str, Any]
    repairs: int
    decisions: dict[str, Any]
    executed: list[dict[str, Any]]
    refusal: str
    final: str


@dataclass
class TurnResult:
    thread_id: str
    status: str  # "done" | "awaiting_approval"
    answer: str
    plan: dict[str, Any] | None
    proposals: list[dict[str, Any]]
    verification: dict[str, Any] | None
    executed: list[dict[str, Any]]
    tool_log: list[dict[str, Any]]
    refused: bool = False
    raw_state: dict[str, Any] = field(default_factory=dict, repr=False)


class Copilot:
    def __init__(self, store: TelemetryStore | None = None, policy: Policy | None = None, llm: LLM | None = None,
                 audit: AuditLog | None = None, outbox_dir: Path | str | None = None):
        self.store = store or TelemetryStore.from_jsonl()
        self.policy = policy or Policy.load()
        self.llm = llm or LLM()
        self.audit = audit or AuditLog(ROOT / "var" / "audit.jsonl")
        self.outbox_dir = Path(outbox_dir or ROOT / "var" / "outbox")
        self._names = {t: t.split("-")[0].lower() for t in self.store.tenants}
        self.graph = self._build()

    # ------------------------------------------------------------ helpers
    def _ctx(self, s: TurnState) -> ToolContext:
        ctx = ToolContext(Session(s["tenant_id"], s["user_id"], s.get("role", "it_admin")),
                          self.store.for_tenant(s["tenant_id"]), self.policy)
        ctx.ledger = dict(s.get("ledger", {}))
        ctx.numbers = set(s.get("numbers", []))
        ctx.proposals = [dict(p) for p in s.get("proposals", [])]
        ctx.tool_log = list(s.get("tool_log", []))
        return ctx

    def _log(self, s: TurnState, event: str, **data) -> None:
        self.audit.write(event, tenant_id=s["tenant_id"], actor=s["user_id"], **data)

    def _foreign(self, tenant_id: str) -> tuple[list[str], list[str]]:
        devs, emps = [], []
        for t in self.store.tenants:
            if t != tenant_id:
                v = self.store.for_tenant(t)
                devs += v.device_ids()
                emps += v.employee_ids()
        return devs, emps

    # ------------------------------------------------------------ nodes
    def input_guard(self, s: TurnState) -> dict[str, Any]:
        """Deterministic pre-check. Runs before any model call, so a cross-tenant
        request never reaches the model at all."""
        q = s["question"]
        ql = q.lower()
        others = [t for t in self.store.tenants if t != s["tenant_id"]]
        hits = [t for t in others if t in ql or re.search(rf"\b{re.escape(self._names[t])}\b", ql)]
        devs, emps = self._foreign(s["tenant_id"])
        hits += [d for d in devs if d.lower() in ql] + [e for e in emps if e in ql]
        self._log(s, "turn_start", question=q, role=s.get("role"))
        if hits:
            msg = (f"I can only access devices that belong to your organisation ({s['tenant_id']}). "
                   "I can't look up, compare with or act on another organisation's devices or people.")
            self._log(s, "input_refused", reason="cross_tenant_reference", matched=hits)
            return {"refusal": msg}
        return {}

    def plan(self, s: TurnState) -> dict[str, Any]:
        msgs = list(s.get("history", [])) + [{"role": "user", "content":
                f"Organisation: {s['tenant_id']}. Admin role: {s.get('role', 'it_admin')}.\n\nRequest: {s['question']}"}]
        resp = self.llm.call(system=PLANNER_SYSTEM, messages=msgs, output_schema=PLAN_SCHEMA, max_tokens=4000)
        try:
            plan = json.loads(text_of(resp))
        except json.JSONDecodeError:
            plan = {"intent": "question", "restated_question": s["question"], "assumptions": [], "steps": [],
                    "may_propose_actions": False, "out_of_scope_reason": "", "_note": "planner output unparseable"}
        self._log(s, "plan", plan=plan)
        if plan.get("intent") == "out_of_scope":
            return {"plan": plan, "refusal": "I can only answer from your fleet's device telemetry. "
                    + (plan.get("out_of_scope_reason") or "")}
        brief = {k: plan[k] for k in ("restated_question", "assumptions", "steps", "may_propose_actions") if k in plan}
        first = {"role": "user", "content": (
            f"Organisation: {s['tenant_id']}. Admin: {s['user_id']} (role {s.get('role', 'it_admin')}).\n\n"
            f"Request: {s['question']}\n\nPlan from the planning step (adapt it if the evidence calls for it):\n"
            f"{json.dumps(brief, indent=1)}")}
        return {"plan": plan, "messages": list(s.get("history", [])) + [first], "rounds": 0, "repairs": 0,
                "ledger": {}, "numbers": [], "proposals": [], "tool_log": [], "executed": []}

    def _tools_for(self, s: TurnState) -> list[dict[str, Any]]:
        # Least privilege per turn: action tools are offered only to admins, and only when the
        # plan says the request is about doing something. A pure question cannot trigger an action.
        allow = s.get("role", "it_admin") == "it_admin" and bool(s.get("plan", {}).get("may_propose_actions"))
        return tool_specs(include_actions=allow)

    def agent(self, s: TurnState) -> dict[str, Any]:
        resp = self.llm.call(system=AGENT_SYSTEM, messages=s["messages"], tools=self._tools_for(s))
        msgs = s["messages"] + [{"role": "assistant", "content": resp["content"]}]
        out: dict[str, Any] = {"messages": msgs, "draft": text_of(resp)}
        if resp.get("stop_reason") == "refusal":
            out["refusal"] = "The model declined this request."
            self._log(s, "model_refusal", stop_details=resp.get("stop_details"))
        return out

    def tools(self, s: TurnState) -> dict[str, Any]:
        ctx = self._ctx(s)
        last = s["messages"][-1]["content"]
        calls = tool_uses({"content": last})
        rounds = s.get("rounds", 0) + 1
        offered = {t["name"] for t in self._tools_for(s)}
        results = []
        for c in calls:
            if rounds > MAX_TOOL_ROUNDS:
                text, err = json.dumps({"error": "Tool budget for this request is used up. Answer with the evidence you have."}), True
            elif c["name"] not in offered:
                text, err = json.dumps({"error": f"Tool {c['name']!r} is not available for this request."}), True
            else:
                text, err = dispatch(ctx, c["name"], c.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": c["id"], "content": text, "is_error": err})
            self._log(s, "tool_call", tool=c["name"], args=c.get("input"), is_error=err,
                      result_preview=text[:300])
            if c["name"] in ACTION_TOOLS:
                last_p = ctx.proposals[-1] if ctx.proposals else {}
                self._log(s, "proposal", tool=c["name"], status=last_p.get("status"),
                          proposal_id=last_p.get("proposal_id"), reason=last_p.get("reason"), args=c.get("input"))
        # All results go back in one user message, which keeps parallel tool calls working.
        return {"messages": s["messages"] + [{"role": "user", "content": results}], "rounds": rounds,
                "ledger": ctx.ledger, "numbers": sorted(ctx.numbers), "proposals": ctx.proposals,
                "tool_log": ctx.tool_log}

    def verify_node(self, s: TurnState) -> dict[str, Any]:
        view = self.store.for_tenant(s["tenant_id"])
        fdev, femp = self._foreign(s["tenant_id"])
        v = verify(s.get("draft", ""), ledger=s.get("ledger", {}), numbers=set(s.get("numbers", [])),
                   tenant_devices=view.device_ids(), tenant_employees=view.employee_ids(),
                   foreign_devices=fdev, foreign_employees=femp, tools_used=len(s.get("tool_log", [])))
        self._log(s, "verification", ok=v.ok, issues=v.issues, citations=len(v.citations))
        out: dict[str, Any] = {"verification": v.to_dict()}
        if not v.ok and s.get("repairs", 0) < MAX_REPAIRS and not v.hard_block:
            issues = "\n".join(f"- {i['code']}: {i['detail']}" for i in v.issues)
            out["messages"] = s["messages"] + [{"role": "user", "content": REPAIR_TEMPLATE.format(issues=issues)}]
            out["repairs"] = s.get("repairs", 0) + 1
        elif any(p["status"] == "pending_approval" for p in s.get("proposals", [])) and not v.hard_block:
            self._log(s, "approval_requested",
                      proposals=[p["proposal_id"] for p in s["proposals"] if p["status"] == "pending_approval"])
        return out

    def approval(self, s: TurnState) -> dict[str, Any]:
        # Note: LangGraph re-runs this node from the top when it resumes, so nothing
        # with side effects may happen before interrupt(). The request is logged in verify_node.
        pending = [p for p in s.get("proposals", []) if p["status"] == "pending_approval"]
        decisions = interrupt({"type": "approval_required", "tenant_id": s["tenant_id"], "proposals": pending})
        clean: dict[str, Any] = {}
        for p in pending:
            d = (decisions or {}).get(p["proposal_id"], {})
            approver = str(d.get("approver", "")).strip()
            ok = bool(d.get("approved")) and approver != "" and approver != "agent"
            clean[p["proposal_id"]] = {"approved": ok, "approver": approver or None, "comment": d.get("comment", "")}
            self._log(s, "approval_decision", proposal_id=p["proposal_id"], approved=ok, approver=approver or None,
                      comment=d.get("comment", ""))
        return {"decisions": clean}

    def execute(self, s: TurnState) -> dict[str, Any]:
        ctx = self._ctx(s)
        executed = []
        proposals = []
        for p in s.get("proposals", []):
            p = dict(p)
            if p["status"] != "pending_approval":
                proposals.append(p)
                continue
            d = s.get("decisions", {}).get(p["proposal_id"], {})
            if not d.get("approved"):
                p["status"] = "rejected_by_human"
                proposals.append(p)
                continue
            decision = recheck(ctx, p)  # time-of-check vs time-of-use: validate again right before running
            if not decision.allowed:
                p["status"] = "blocked_at_execution"
                p["reason"] = decision.reason
                self._log(s, "action_blocked_at_execution", proposal_id=p["proposal_id"], reason=decision.reason)
                proposals.append(p)
                continue
            record = {"action_id": "A-" + uuid.uuid4().hex[:10], "proposal_id": p["proposal_id"], "tool": p["tool"],
                      "args": p["args"], "tenant_id": s["tenant_id"], "approved_by": d["approver"],
                      "executed_at": datetime.now(timezone.utc).isoformat()}
            self.outbox_dir.mkdir(parents=True, exist_ok=True)
            with open(self.outbox_dir / f"{s['tenant_id']}.jsonl", "a") as fh:
                fh.write(json.dumps(record) + "\n")
            p["status"] = "executed"
            p["action_id"] = record["action_id"]
            executed.append(record)
            proposals.append(p)
            self._log(s, "action_executed", action=record)
        return {"executed": executed, "proposals": proposals}

    def finalize(self, s: TurnState) -> dict[str, Any]:
        if s.get("refusal") and not s.get("draft"):
            final = s["refusal"]
        else:
            v = s.get("verification") or {}
            final = s.get("draft", "")
            if v.get("hard_block"):
                final = ("I can't show this answer because it referenced data outside your organisation. "
                         "The attempt has been logged.")
            elif v and not v.get("ok"):
                final += "\n\n> Grounding check: " + "; ".join(i["detail"] for i in v["issues"])
            props = s.get("proposals", [])
            if any(p["status"] in ("executed", "rejected_by_human", "blocked_at_execution") for p in props):
                lines = []
                for p in props:
                    if p["status"] == "executed":
                        lines.append(f"- {p['proposal_id']} {p['tool']}: executed as {p['action_id']}")
                    elif p["status"] == "rejected_by_human":
                        lines.append(f"- {p['proposal_id']} {p['tool']}: not approved, nothing done")
                    elif p["status"] == "blocked_at_execution":
                        lines.append(f"- {p['proposal_id']} {p['tool']}: blocked at execution ({p['reason']})")
                final += "\n\n**Action outcome**\n" + "\n".join(lines)
        self._log(s, "turn_end", answer_chars=len(final), proposals=len(s.get("proposals", [])),
                  executed=len(s.get("executed", [])), tool_calls=len(s.get("tool_log", [])))
        return {"final": final}

    # ------------------------------------------------------------ routing
    @staticmethod
    def _after_guard(s: TurnState) -> str:
        return "finalize" if s.get("refusal") else "plan"

    @staticmethod
    def _after_plan(s: TurnState) -> str:
        return "finalize" if s.get("refusal") else "agent"

    @staticmethod
    def _after_agent(s: TurnState) -> str:
        if s.get("refusal"):
            return "finalize"
        last = s["messages"][-1]["content"]
        return "tools" if tool_uses({"content": last}) else "verify"

    @staticmethod
    def _after_verify(s: TurnState) -> str:
        if s["messages"][-1]["role"] == "user":  # a repair request was appended
            return "agent"
        if any(p["status"] == "pending_approval" for p in s.get("proposals", [])) and not (s.get("verification") or {}).get("hard_block"):
            return "approval"
        return "finalize"

    def _build(self):
        g = StateGraph(TurnState)
        g.add_node("input_guard", self.input_guard)
        g.add_node("plan", self.plan)
        g.add_node("agent", self.agent)
        g.add_node("tools", self.tools)
        g.add_node("verify", self.verify_node)
        g.add_node("approval", self.approval)
        g.add_node("execute", self.execute)
        g.add_node("finalize", self.finalize)
        g.add_edge(START, "input_guard")
        g.add_conditional_edges("input_guard", self._after_guard, ["plan", "finalize"])
        g.add_conditional_edges("plan", self._after_plan, ["agent", "finalize"])
        g.add_conditional_edges("agent", self._after_agent, ["tools", "verify", "finalize"])
        g.add_edge("tools", "agent")
        g.add_conditional_edges("verify", self._after_verify, ["agent", "approval", "finalize"])
        g.add_edge("approval", "execute")
        g.add_edge("execute", "finalize")
        g.add_edge("finalize", END)
        return g.compile(checkpointer=InMemorySaver())

    # ------------------------------------------------------------ public API
    def ask(self, session: Session, question: str, history: list[dict[str, Any]] | None = None,
            thread_id: str | None = None) -> TurnResult:
        thread_id = thread_id or uuid.uuid4().hex[:12]
        state: TurnState = {"tenant_id": session.tenant_id, "user_id": session.user_id, "role": session.role,
                            "question": question, "history": history or []}
        out = self.graph.invoke(state, {"configurable": {"thread_id": thread_id}})
        return self._result(thread_id, out)

    def resume(self, thread_id: str, decisions: dict[str, Any]) -> TurnResult:
        out = self.graph.invoke(Command(resume=decisions), {"configurable": {"thread_id": thread_id}})
        return self._result(thread_id, out)

    def _result(self, thread_id: str, out: dict[str, Any]) -> TurnResult:
        waiting = bool(out.get("__interrupt__"))
        return TurnResult(
            thread_id=thread_id, status="awaiting_approval" if waiting else "done",
            answer=out.get("draft", "") if waiting else out.get("final", ""),
            plan=out.get("plan"), proposals=out.get("proposals", []), verification=out.get("verification"),
            executed=out.get("executed", []), tool_log=out.get("tool_log", []),
            refused=bool(out.get("refusal")) and not out.get("draft"), raw_state=out)
