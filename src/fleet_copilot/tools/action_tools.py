"""State-changing tools. None of them changes state.

Calling one creates a *proposal* after the action policy has checked it. The agent
graph pauses for explicit human approval, re-checks the proposal and only then runs
the executor (see agent/executor.py). The model never holds a code path that
performs an action directly.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..guardrails import action_policy as ap
from .context import ToolContext

EVIDENCE_DESC = ("Finding ids (e.g. 'ram_pressure/ABC123') and/or snapshot ids (e.g. 'ABC123/2026-06-12') returned by "
                 "read tools in this turn that justify the action.")


class UpgradeArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    component: Literal["storage", "ram", "battery"]
    spec: str = Field(..., description="Target spec, e.g. '32 GB' for RAM, '1 TB' for storage, 'OEM replacement' for battery.")
    justification: str = Field(..., description="One or two sentences an approver can read.")
    evidence: list[str] = Field(..., min_length=1, description=EVIDENCE_DESC)


class TicketArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    check_id: str = Field(..., description="A failing compliance check id, 'software:<Name>' for risky software, or 'disk_cleanup'.")
    note: str = Field(..., description="What is wrong and what the technician should do.")
    evidence: list[str] = Field(..., min_length=1, description=EVIDENCE_DESC)


class ReplaceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    reason: str
    evidence: list[str] = Field(..., min_length=1, description=EVIDENCE_DESC)


class NotifyArgs(BaseModel):
    model_config = {"extra": "forbid"}
    employee_id: str
    message: str = Field(..., description="Plain-language message to the employee about their own device only.")
    evidence: list[str] = Field(..., min_length=1, description=EVIDENCE_DESC)


def _propose(ctx: ToolContext, tool: str, args: dict[str, Any], decision: ap.PolicyDecision) -> dict[str, Any]:
    if not decision.allowed:
        ctx.proposals.append({"tool": tool, "args": args, "status": "blocked_by_policy", "reason": decision.reason})
        return {"status": "blocked_by_policy", "reason": decision.reason,
                "guidance": "Do not retry the same action. Explain to the admin why it was blocked, or choose an action the evidence supports."}
    limit = ctx.policy.actions["limits"]["max_proposals_per_turn"]
    pending = [p for p in ctx.proposals if p["status"] == "pending_approval"]
    if len(pending) >= limit:
        return {"status": "blocked_by_policy", "reason": f"At most {limit} action proposals per turn."}
    key = json.dumps({"tool": tool, **{k: v for k, v in args.items() if k not in ("evidence", "justification", "note", "message", "reason")}}, sort_keys=True)
    pid = "P-" + hashlib.sha256(key.encode()).hexdigest()[:8]
    if any(p.get("proposal_id") == pid for p in pending):
        return {"status": "duplicate", "proposal_id": pid, "reason": "An identical action is already awaiting approval."}
    p = {"proposal_id": pid, "tool": tool, "args": args, "status": "pending_approval",
         "policy_facts": decision.facts, "tenant_id": ctx.session.tenant_id, "proposed_by": "agent",
         "on_behalf_of": ctx.session.user_id}
    ctx.proposals.append(p)
    return {"status": "pending_approval", "proposal_id": pid, "policy_facts": decision.facts,
            "note": "Nothing has been executed. A human must approve this proposal."}


def create_upgrade_order(ctx: ToolContext, a: UpgradeArgs) -> dict[str, Any]:
    return _propose(ctx, "create_upgrade_order", a.model_dump(),
                    ap.check_upgrade_order(ctx, a.device_id, a.component, a.spec, a.evidence))


def open_remediation_ticket(ctx: ToolContext, a: TicketArgs) -> dict[str, Any]:
    return _propose(ctx, "open_remediation_ticket", a.model_dump(),
                    ap.check_remediation_ticket(ctx, a.device_id, a.check_id, a.note, a.evidence))


def flag_device_for_replacement(ctx: ToolContext, a: ReplaceArgs) -> dict[str, Any]:
    return _propose(ctx, "flag_device_for_replacement", a.model_dump(),
                    ap.check_replacement(ctx, a.device_id, a.reason, a.evidence))


def notify_employee(ctx: ToolContext, a: NotifyArgs) -> dict[str, Any]:
    return _propose(ctx, "notify_employee", a.model_dump(),
                    ap.check_notify(ctx, a.employee_id, a.message, a.evidence))


def recheck(ctx: ToolContext, proposal: dict[str, Any]) -> ap.PolicyDecision:
    """Re-run the policy just before execution."""
    a = proposal["args"]
    t = proposal["tool"]
    if t == "create_upgrade_order":
        return ap.check_upgrade_order(ctx, a["device_id"], a["component"], a["spec"], a["evidence"])
    if t == "open_remediation_ticket":
        return ap.check_remediation_ticket(ctx, a["device_id"], a["check_id"], a["note"], a["evidence"])
    if t == "flag_device_for_replacement":
        return ap.check_replacement(ctx, a["device_id"], a["reason"], a["evidence"])
    if t == "notify_employee":
        return ap.check_notify(ctx, a["employee_id"], a["message"], a["evidence"])
    return ap.PolicyDecision(False, f"unknown action {t}", {})


ACTION_TOOLS = {
    "create_upgrade_order": (create_upgrade_order, UpgradeArgs,
        "PROPOSE a hardware upgrade (storage, ram or battery). Requires a cited finding for the device and checks the part can physically be fitted. Creates a proposal for human approval; executes nothing."),
    "open_remediation_ticket": (open_remediation_ticket, TicketArgs,
        "PROPOSE an IT ticket for a failing compliance check, risky software ('software:<Name>') or disk cleanup ('disk_cleanup'). Creates a proposal for human approval; executes nothing."),
    "flag_device_for_replacement": (flag_device_for_replacement, ReplaceArgs,
        "PROPOSE flagging a device for replacement. Allowed only with findings in two hardware dimensions, or chronic memory pressure on non-upgradeable hardware. Creates a proposal for human approval; executes nothing."),
    "notify_employee": (notify_employee, NotifyArgs,
        "PROPOSE a message to an employee about their own device. Creates a proposal for human approval; sends nothing."),
}
