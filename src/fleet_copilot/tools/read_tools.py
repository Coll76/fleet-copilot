"""Read-only tools exposed to the agent.

Design rules:
1. No tool takes a company_id. The tenant comes from the session, so the model has
   no parameter it could set to reach another tenant.
2. Every result carries the snapshot ids or finding ids it is based on, and those ids
   are written to the evidence ledger.
3. Tools return facts and the thresholds used, never prose conclusions. The model
   writes the conclusion; the verifier checks it against these facts.
4. Results are bounded (max rows) so a broad question cannot flood the context.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ..analytics.detectors import DETECTORS, compliance_drift_fleet, run_detectors
from ..data.models import parse_version
from ..data.store import NotFound
from .context import ToolContext

MAX_ROWS = 50
METRICS = ["disk_free_pct", "disk_free_gb", "mem_used_pct", "battery_cycle_count", "battery_full_charge_capacity",
           "battery_condition", "compliance", "installed_software", "os_version"]


class ToolError(Exception):
    """Returned to the model as an is_error tool_result so it can correct itself."""


# ---------------------------------------------------------------- argument models
class NoArgs(BaseModel):
    model_config = {"extra": "forbid"}


class QueryDevicesArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_class: Literal["laptop", "desktop"] | None = Field(None, description="laptop or desktop (Mac mini is a desktop).")
    os_name: Literal["macOS", "Windows"] | None = None
    os_version_below: str | None = Field(None, description="Major version, e.g. '15' for macOS or '11' for Windows. Requires os_name.")
    disk_free_pct_below: float | None = Field(None, ge=0, le=100)
    mem_used_pct_at_least: float | None = Field(None, ge=0, le=100)
    failing_check: str | None = Field(None, description="A compliance check_id that must be failing, e.g. 'screen_lock'.")
    failing_severity: Literal["low", "medium", "high"] | None = Field(None, description="Only devices failing at least one check of this severity.")
    has_software: str | None = Field(None, description="Exact installed software name, e.g. 'uTorrent'.")
    battery_condition: str | None = Field(None, description="e.g. 'Service Recommended'.")
    employee_id: str | None = None
    device_ids: list[str] | None = None

    @model_validator(mode="after")
    def _os_version_needs_os(self):
        if self.os_version_below and not self.os_name:
            raise ValueError("os_version_below needs os_name, because macOS and Windows version numbers are not comparable")
        return self


class DeviceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str


class HistoryArgs(BaseModel):
    model_config = {"extra": "forbid"}
    device_id: str
    metrics: list[Literal[*METRICS]] = Field(..., min_length=1)  # type: ignore[valid-type]
    last_n: int | None = Field(None, ge=1, le=30, description="Only the most recent N snapshots. Omit for the full window.")


class InsightArgs(BaseModel):
    model_config = {"extra": "forbid"}
    kinds: list[Literal[*DETECTORS]] | None = Field(None, description="Detectors to run. Omit to run all.")  # type: ignore[valid-type]
    device_id: str | None = Field(None, description="Restrict findings to one device.")
    min_severity: Literal["critical", "high", "medium", "low", "info"] | None = None


class ComplianceArgs(BaseModel):
    model_config = {"extra": "forbid"}
    check_id: str | None = Field(None, description="Limit to one check, e.g. 'screen_lock'. Omit for all checks.")


# ---------------------------------------------------------------- helpers
def _brief(s) -> dict[str, Any]:
    return {"device_id": s.device_id, "snapshot_id": s.snapshot_id, "employee_id": s.employee_id,
            "model": s.model_name, "device_class": s.device_class, "os": f"{s.os_name} {s.os_version}"}


def _get_latest(ctx: ToolContext, device_id: str):
    try:
        return ctx.view.latest(device_id)
    except NotFound:
        # Same message for "does not exist" and "belongs to another tenant".
        raise ToolError(f"Device {device_id!r} was not found in your organisation's fleet.") from None


# ---------------------------------------------------------------- tools
def fleet_overview(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    latest = ctx.view.latest_all()
    for s in latest:
        ctx.record_snapshot(s.snapshot_id, _brief(s))
    first, last = ctx.view.window
    return {
        "tenant": ctx.view.company_id,
        "telemetry_window": {"first_day": first, "last_day": last},
        "device_count": len(latest),
        "employee_count": len(ctx.view.employee_ids()),
        "by_os": _count(f"{s.os_name} {s.os_version}" for s in latest),
        "by_model": _count(s.model_name for s in latest),
        "by_class": _count(s.device_class for s in latest),
        "devices": [_brief(s) for s in latest],
    }


def query_devices(ctx: ToolContext, a: QueryDevicesArgs) -> dict[str, Any]:
    """Filter devices on their LATEST snapshot."""
    rows = []
    sev_fail_counts: dict[str, int] = {}
    latest = ctx.view.latest_all()
    for s in latest:
        for c in s.compliance:
            if c.status == "fail":
                sev_fail_counts[c.severity] = sev_fail_counts.get(c.severity, 0) + 1
        if a.device_ids and s.device_id not in a.device_ids:
            continue
        if a.device_class and s.device_class != a.device_class:
            continue
        if a.os_name and s.os_name != a.os_name:
            continue
        if a.os_version_below and not (s.os_version_tuple[:1] < parse_version(a.os_version_below)[:1]):
            continue
        if a.disk_free_pct_below is not None and not s.disk_free_pct < a.disk_free_pct_below:
            continue
        if a.mem_used_pct_at_least is not None and not s.mem_used_pct >= a.mem_used_pct_at_least:
            continue
        if a.failing_check and not ((c := s.check(a.failing_check)) and c.status == "fail"):
            continue
        if a.failing_severity and not any(c.status == "fail" and c.severity == a.failing_severity for c in s.compliance):
            continue
        if a.has_software and not any(n.lower() == a.has_software.lower() for n, _ in s.software):
            continue
        if a.battery_condition and not (s.battery and s.battery.condition.lower() == a.battery_condition.lower()):
            continue
        if a.employee_id and s.employee_id != a.employee_id:
            continue
        row = _brief(s)
        row.update({"disk_free_pct": round(s.disk_free_pct, 1), "disk_free_gb": round(s.disk_free_gb, 1),
                    "disk_size_gb": round(s.disk_size_gb), "mem_used_pct": round(s.mem_used_pct, 1),
                    "ram_gb": round(s.ram_gb),
                    "failing_checks": [f"{c.check_id} ({c.severity})" for c in s.compliance if c.status == "fail"]})
        if s.battery:
            row["battery"] = {"condition": s.battery.condition, "cycle_count": s.battery.cycle_count}
        rows.append(row)
        ctx.record_snapshot(s.snapshot_id, row)
    out = {
        "as_of": "latest snapshot of each device",
        "filters_applied": a.model_dump(exclude_none=True),
        "devices_considered": len(latest),
        "match_count": len(rows),
        "matches": rows[:MAX_ROWS],
        "truncated": len(rows) > MAX_ROWS,
        "tenant_failing_checks_by_severity": sev_fail_counts,
    }
    if a.os_version_below:
        out["note"] = f"Only {a.os_name} devices are compared. Devices on other operating systems are excluded, not counted as older."
    return out


def get_device(ctx: ToolContext, a: DeviceArgs) -> dict[str, Any]:
    s = _get_latest(ctx, a.device_id)
    d = s.summary()
    ctx.record_snapshot(s.snapshot_id, d)
    d["snapshots_available"] = len(ctx.view.history(a.device_id))
    return d


def get_device_history(ctx: ToolContext, a: HistoryArgs) -> dict[str, Any]:
    _get_latest(ctx, a.device_id)
    h = ctx.view.history(a.device_id)
    if a.last_n:
        h = h[-a.last_n:]
    series = []
    for s in h:
        p: dict[str, Any] = {"snapshot_id": s.snapshot_id, "day": s.day}
        for m in a.metrics:
            if m == "disk_free_pct":
                p[m] = round(s.disk_free_pct, 1)
            elif m == "disk_free_gb":
                p[m] = round(s.disk_free_gb, 1)
            elif m == "mem_used_pct":
                p[m] = round(s.mem_used_pct, 1)
            elif m == "battery_cycle_count":
                p[m] = s.battery.cycle_count if s.battery else None
            elif m == "battery_full_charge_capacity":
                p[m] = s.battery.full_charge_capacity if s.battery else None
            elif m == "battery_condition":
                p[m] = s.battery.condition if s.battery else None
            elif m == "compliance":
                p[m] = {c.check_id: c.status for c in s.compliance}
            elif m == "installed_software":
                p[m] = [n for n, _ in s.software]
            elif m == "os_version":
                p[m] = f"{s.os_name} {s.os_version}"
        series.append(p)
        ctx.record_snapshot(s.snapshot_id, p)
    gaps = sum(1 for s in h if s.battery_missing_on_laptop)
    out = {"device_id": a.device_id, "points": len(series), "series": series}
    if gaps and any(m.startswith("battery") for m in a.metrics):
        out["note"] = f"{gaps} snapshot(s) have no battery block. This laptop's agent skipped battery collection; treat null as missing data, not as a removed battery."
    return out


def run_insights(ctx: ToolContext, a: InsightArgs) -> dict[str, Any]:
    from ..analytics.detectors import SEVERITY_ORDER
    if a.device_id:
        _get_latest(ctx, a.device_id)
    found = run_detectors(ctx.view, ctx.policy, list(a.kinds) if a.kinds else None)
    if a.device_id:
        found = [f for f in found if f.device_id == a.device_id]
    if a.min_severity:
        found = [f for f in found if SEVERITY_ORDER[f.severity] <= SEVERITY_ORDER[a.min_severity]]
    for f in found:
        ctx.record_finding(f)
    return {
        "detectors_run": list(a.kinds) if a.kinds else sorted(DETECTORS),
        "finding_count": len(found),
        "findings": [f.to_dict() for f in found[:MAX_ROWS]],
        "truncated": len(found) > MAX_ROWS,
        "how_to_cite": "Cite finding_id for the conclusion and snapshot ids in 'evidence' for the underlying values.",
    }


def compliance_overview(ctx: ToolContext, a: ComplianceArgs) -> dict[str, Any]:
    latest = ctx.view.latest_all()
    checks: dict[str, dict[str, Any]] = {}
    for s in latest:
        for c in s.compliance:
            if a.check_id and c.check_id != a.check_id:
                continue
            e = checks.setdefault(c.check_id, {"check_id": c.check_id, "severity": c.severity, "passing": 0,
                                               "failing": 0, "failing_devices": []})
            if c.status == "fail":
                e["failing"] += 1
                e["failing_devices"].append({"device_id": s.device_id, "snapshot_id": s.snapshot_id})
                ctx.record_snapshot(s.snapshot_id, {"check": c.check_id, "status": "fail"})
            else:
                e["passing"] += 1
    if a.check_id and not checks:
        raise ToolError(f"No compliance check named {a.check_id!r}. Known checks: "
                        f"{sorted({c.check_id for s in latest for c in s.compliance})}")
    drift = compliance_drift_fleet(ctx.view)
    trend = {cid: {"failing_devices_first_day": drift["first_day"].get(cid, 0),
                   "failing_devices_last_day": drift["last_day"].get(cid, 0)} for cid in checks}
    return {"as_of": "latest snapshot of each device", "window": drift["window"], "devices": len(latest),
            "checks": list(checks.values()), "trend_over_window": trend}


def get_policy(ctx: ToolContext, _: NoArgs) -> dict[str, Any]:
    return {"thresholds": ctx.policy.t, "risky_software": ctx.policy.risky_software}


def _count(it) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return dict(sorted(out.items()))


READ_TOOLS = {
    "fleet_overview": (fleet_overview, NoArgs,
        "List every device in the admin's fleet with model, OS, owner and latest snapshot id, plus counts by OS and model. Use first when the question is broad."),
    "query_devices": (query_devices, QueryDevicesArgs,
        "Filter devices on their latest snapshot (disk %, memory %, OS version, device class, failing compliance checks, installed software, battery condition, owner). Use for 'which/how many devices' questions."),
    "get_device": (get_device, DeviceArgs,
        "Full latest snapshot of one device: hardware, OS, memory, disk, battery, compliance results and installed software."),
    "get_device_history": (get_device_history, HistoryArgs,
        "Time series of selected metrics for one device across the 30-day window. Use to verify a trend or find when something changed."),
    "run_insights": (run_insights, InsightArgs,
        "Run deterministic detectors (disk_low, disk_trend, ram_pressure, battery_eol, compliance_drift, risky_software, telemetry_gap). Each finding has the finding, metrics, evidence snapshot ids and the policy threshold used."),
    "compliance_overview": (compliance_overview, ComplianceArgs,
        "Pass and fail counts per compliance check on the latest snapshots, the failing devices, and how the failing count changed over the window."),
    "get_policy": (get_policy, NoArgs,
        "The thresholds that define 'low disk', 'RAM-constrained', 'battery end-of-life' and the risky-software list. Quote these when explaining a definition."),
}
