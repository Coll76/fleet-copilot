"""Evidence and safety policy for state-changing actions.

The model can ask for an action, but this module decides whether the request is
even allowed to reach a human for approval. It re-derives every fact from the
telemetry instead of trusting the model's arguments:

  * the device and employee must belong to the session's tenant;
  * the role must be allowed to propose actions;
  * every cited evidence id must have been returned by a tool in this turn;
  * the cited evidence must include a detector finding that justifies this action
    for this device (e.g. a RAM upgrade needs a ram_pressure finding);
  * the action must make physical sense (no RAM upgrade on soldered RAM, the new
    spec must be bigger than the current one);
  * replacement needs problems in two hardware dimensions, or chronic memory
    pressure on hardware that cannot be upgraded;
  * employee messages must not mention other people's devices or employee ids.

The same check runs twice: when the proposal is created and again just before it
executes after approval, in case the data changed in between.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..analytics.detectors import Finding
from ..tools.context import ToolContext

COMPONENTS = ("storage", "ram", "battery")


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    facts: dict[str, Any]


def _deny(reason: str, **facts) -> PolicyDecision:
    return PolicyDecision(False, reason, facts)


def _allow(reason: str, **facts) -> PolicyDecision:
    return PolicyDecision(True, reason, facts)


def _parse_gb(spec: str) -> int | None:
    m = re.search(r"(\d+)\s*(tb|gb)", spec.lower())
    if not m:
        return None
    n = int(m.group(1))
    return n * 1024 if m.group(2) == "tb" else n


def _common(ctx: ToolContext, device_id: str | None, evidence: list[str]) -> PolicyDecision | None:
    if ctx.session.role != "it_admin":
        return _deny(f"Role {ctx.session.role!r} may read fleet data but may not propose actions.")
    if device_id is not None and not ctx.view.owns_device(device_id):
        return _deny(f"Device {device_id!r} was not found in your organisation's fleet.")
    if not evidence:
        return _deny("No evidence cited. Cite the finding ids or snapshot ids that justify this action.")
    unseen = [e for e in evidence if e not in ctx.ledger]
    if unseen:
        return _deny(f"Evidence {unseen} was not returned by any tool in this conversation turn. "
                     "Retrieve it with a tool first, then cite the ids exactly as returned.")
    if device_id is not None and not any(device_id in e for e in evidence):
        return _deny(f"None of the cited evidence is about device {device_id}.")
    return None


def _device_findings(ctx: ToolContext, device_id: str) -> dict[str, Finding]:
    return {fid: f for fid, f in ctx.findings().items() if f.device_id == device_id}


def _cited_findings(ctx: ToolContext, device_id: str, evidence: list[str], kinds: list[str]) -> list[Finding]:
    fs = _device_findings(ctx, device_id)
    return [fs[e] for e in evidence if e in fs and fs[e].kind in kinds]


def _upgradeable(ctx: ToolContext, model: str) -> dict[str, bool]:
    return ctx.policy.raw.get("hardware", {}).get("upgradeable", {}).get(
        model, {"ram": False, "storage": False, "battery": False})


# ---------------------------------------------------------------- per action
def check_upgrade_order(ctx: ToolContext, device_id: str, component: str, spec: str, evidence: list[str]) -> PolicyDecision:
    if (d := _common(ctx, device_id, evidence)):
        return d
    if component not in COMPONENTS:
        return _deny(f"component must be one of {COMPONENTS}.")
    s = ctx.view.latest(device_id)
    kinds = ctx.policy.actions["create_upgrade_order"]["components"][component]
    cited = _cited_findings(ctx, device_id, evidence, kinds)
    if not cited:
        return _deny(f"A {component} upgrade needs a cited finding of kind {kinds} for {device_id}. "
                     f"Run run_insights and cite the finding_id.")
    up = _upgradeable(ctx, s.model_name)
    if not up.get(component, False):
        alt = {"ram": "flag_device_for_replacement if the memory pressure is chronic",
               "storage": "open_remediation_ticket with check_id 'disk_cleanup'",
               "battery": "a hardware service ticket"}[component]
        return _deny(f"{component} is not field-upgradeable on a {s.model_name} ({s.raw['device_identity'].get('processor')}). "
                     f"Consider {alt} instead.", model=s.model_name)
    if component in ("ram", "storage"):
        gb = _parse_gb(spec)
        if gb is None:
            return _deny("spec must state a size, e.g. '32 GB' or '1 TB'.")
        current = round(s.ram_gb) if component == "ram" else round(s.disk_size_gb)
        options = ctx.policy.raw["hardware"]["ram_options_gb" if component == "ram" else "storage_options_gb"]
        if gb <= current:
            return _deny(f"Requested {gb} GB is not larger than the current {current} GB.")
        if gb not in options:
            return _deny(f"{gb} GB is not an orderable size. Options: {options} GB.")
        return _allow("ok", current_gb=current, requested_gb=gb, model=s.model_name,
                      justified_by=[f.finding_id for f in cited])
    return _allow("ok", model=s.model_name, justified_by=[f.finding_id for f in cited])


def check_remediation_ticket(ctx: ToolContext, device_id: str, check_id: str, note: str, evidence: list[str]) -> PolicyDecision:
    if (d := _common(ctx, device_id, evidence)):
        return d
    if not note.strip():
        return _deny("note must describe the problem and the fix requested.")
    s = ctx.view.latest(device_id)
    if check_id.startswith("software:"):
        name = check_id.split(":", 1)[1]
        cited = [f for f in _cited_findings(ctx, device_id, evidence, ["risky_software"]) if f.metrics["software"] == name]
        if not cited:
            return _deny(f"A software ticket needs a cited risky_software finding for {name!r} on {device_id}.")
        return _allow("ok", justified_by=[f.finding_id for f in cited])
    if check_id == "disk_cleanup":
        cited = _cited_findings(ctx, device_id, evidence, ["disk_low", "disk_trend"])
        if not cited:
            return _deny(f"A disk cleanup ticket needs a cited disk_low or disk_trend finding for {device_id}.")
        return _allow("ok", justified_by=[f.finding_id for f in cited], disk_free_pct=round(s.disk_free_pct, 1))
    c = s.check(check_id)
    if c is None:
        return _deny(f"{device_id} does not report a compliance check named {check_id!r}. "
                     f"It reports {[x.check_id for x in s.compliance]}.")
    if c.status != "fail":
        return _deny(f"{check_id} is currently passing on {device_id} (latest snapshot {s.snapshot_id}). "
                     "No remediation is needed.")
    if s.snapshot_id not in evidence and not any(e.startswith(f"compliance_drift/{device_id}/{check_id}") for e in evidence):
        return _deny(f"Cite the latest snapshot {s.snapshot_id} or the compliance_drift finding that shows {check_id} failing.")
    return _allow("ok", check_severity=c.severity, latest_snapshot=s.snapshot_id)


def check_replacement(ctx: ToolContext, device_id: str, reason: str, evidence: list[str]) -> PolicyDecision:
    if (d := _common(ctx, device_id, evidence)):
        return d
    cfg = ctx.policy.actions["flag_device_for_replacement"]
    s = ctx.view.latest(device_id)
    dims = {dim: [f for f in _device_findings(ctx, device_id).values() if f.kind in kinds]
            for dim, kinds in cfg["hardware_dimensions"].items()}
    present = {dim: fs for dim, fs in dims.items() if fs}
    cited_ids = set(evidence)
    if not any(f.finding_id in cited_ids for fs in present.values() for f in fs):
        return _deny(f"Replacement needs cited hardware findings for {device_id}. Found dimensions: {sorted(present)}.")
    up = _upgradeable(ctx, s.model_name)
    comp_of = {"memory": "ram", "storage": "storage", "battery": "battery"}
    stuck = [dim for dim in present if dim in cfg["replace_if_not_upgradeable"] and not up.get(comp_of[dim], False)]
    if len(present) >= cfg["min_distinct_hardware_dimensions"]:
        return _allow("ok", dimensions=sorted(present), basis="multiple hardware dimensions")
    if stuck:
        return _allow("ok", dimensions=sorted(present), basis=f"chronic {stuck[0]} pressure on a {s.model_name}, which cannot be upgraded")
    return _deny(f"Only {sorted(present) or 'no'} hardware dimension(s) affected on {device_id}, and they can be fixed by "
                 "an upgrade or cleanup. Propose create_upgrade_order or open_remediation_ticket instead.",
                 dimensions=sorted(present))


def check_notify(ctx: ToolContext, employee_id: str, message: str, evidence: list[str]) -> PolicyDecision:
    if (d := _common(ctx, None, evidence)):
        return d
    if not ctx.view.owns_employee(employee_id):
        return _deny(f"Employee {employee_id!r} was not found in your organisation.")
    limit = ctx.policy.actions["notify_employee"]["max_message_chars"]
    if not message.strip() or len(message) > limit:
        return _deny(f"Message must be 1 to {limit} characters.")
    own = {d for d in ctx.view.device_ids() if ctx.view.latest(d).employee_id == employee_id}
    others = [d for d in ctx.view.device_ids() if d not in own and d in message]
    other_emps = [e for e in ctx.view.employee_ids() if e != employee_id and e in message]
    if others or other_emps:
        return _deny(f"Message mentions devices or people that are not this employee's ({others + other_emps}). "
                     "Employees only receive information about their own devices.")
    about = [e for e in evidence if any(d in e for d in own)]
    if not about:
        return _deny(f"Cite evidence about one of {employee_id}'s own devices ({sorted(own)}).")
    return _allow("ok", employee_devices=sorted(own))
