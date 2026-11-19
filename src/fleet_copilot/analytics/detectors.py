"""Deterministic insight detectors.

Design choice: trends are computed in code, not by the LLM. Language models are poor
at arithmetic over 30-point time series and cannot be audited when they get it wrong.
Each detector returns Findings that carry (a) the metrics that triggered them,
(b) the exact snapshot ids used as evidence, and (c) the policy threshold applied.
The LLM's job is to choose which detectors to run, explain the results and decide
what to do about them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from statistics import mean
from typing import Any, Callable

from ..data.models import Snapshot
from ..data.store import TenantView
from .policy import Policy

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass(frozen=True)
class Finding:
    finding_id: str  # "<kind>/<device_id>[/<qualifier>]"
    kind: str
    device_id: str
    employee_id: str
    severity: str
    title: str
    explanation: str
    metrics: dict[str, Any]
    evidence: tuple[str, ...]  # snapshot ids
    policy: dict[str, Any] = field(default_factory=dict)  # thresholds applied

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = list(self.evidence)
        return d


def _linfit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Least-squares slope and intercept. Returns (slope, intercept)."""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    mx, my = mean(xs), mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = 0.0 if den == 0 else sum((x - mx) * (y - my) for x, y in points) / den
    return slope, my - slope * mx


def _days(h: list[Snapshot]) -> list[float]:
    t0 = h[0].collected_at
    return [(s.collected_at - t0).total_seconds() / 86400 for s in h]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:] + "."


def _sample(h: list[Snapshot], k: int = 4) -> tuple[str, ...]:
    """First, last and a few evenly spaced snapshots: enough to verify a trend
    without flooding the context window."""
    if len(h) <= k:
        return tuple(s.snapshot_id for s in h)
    idx = sorted({round(i * (len(h) - 1) / (k - 1)) for i in range(k)})
    return tuple(h[i].snapshot_id for i in idx)


# ---------------------------------------------------------------- storage
def detect_disk_low(view: TenantView, p: Policy) -> list[Finding]:
    thr = p.t["disk_low_free_pct"]
    out = []
    for d in view.device_ids():
        s = view.latest(d)
        if s.disk_free_pct < thr:
            out.append(Finding(
                finding_id=f"disk_low/{d}", kind="disk_low", device_id=d, employee_id=s.employee_id,
                severity="critical" if s.disk_free_pct < thr / 2 else "high",
                title=f"Low disk space on {s.model_name} ({d})",
                explanation=f"Latest snapshot shows {s.disk_free_pct:.1f}% free ({s.disk_free_gb:.1f} GB of {s.disk_size_gb:.0f} GB), below the {thr}% policy threshold.",
                metrics={"disk_free_pct": round(s.disk_free_pct, 1), "disk_free_gb": round(s.disk_free_gb, 1),
                         "disk_size_gb": round(s.disk_size_gb)},
                evidence=(s.snapshot_id,), policy={"disk_low_free_pct": thr}))
    return out


def detect_disk_trend(view: TenantView, p: Policy) -> list[Finding]:
    horizon = p.t["disk_trend_horizon_days"]
    min_pts = p.t["disk_trend_min_points"]
    out = []
    for d in view.device_ids():
        h = view.history(d)
        if len(h) < min_pts:
            continue
        slope, _ = _linfit(list(zip(_days(h), (s.disk_free_gb for s in h))))
        last = h[-1]
        if slope >= -0.05:  # flat or growing; ignore noise below 50 MB/day
            continue
        days_to_full = last.disk_free_gb / -slope
        consumed = h[0].disk_free_gb - last.disk_free_gb
        if days_to_full > horizon:
            continue
        out.append(Finding(
            finding_id=f"disk_trend/{d}", kind="disk_trend", device_id=d, employee_id=last.employee_id,
            severity="critical" if days_to_full < 7 else "high",
            title=f"Storage filling up on {last.model_name} ({d})",
            explanation=(f"Free space fell from {h[0].disk_free_gb:.1f} GB to {last.disk_free_gb:.1f} GB over {len(h)} snapshots "
                         f"(about {-slope:.1f} GB/day by linear fit). At that rate the disk is full in about {days_to_full:.0f} days."),
            metrics={"slope_gb_per_day": round(slope, 2), "free_gb_first": round(h[0].disk_free_gb, 1),
                     "free_gb_last": round(last.disk_free_gb, 1), "consumed_gb": round(consumed, 1),
                     "days_to_full": round(days_to_full, 1), "points": len(h)},
            evidence=_sample(h), policy={"disk_trend_horizon_days": horizon}))
    return out


# ---------------------------------------------------------------- memory
def detect_ram_pressure(view: TenantView, p: Policy) -> list[Finding]:
    thr = p.t["ram_pressure_used_pct"]
    share_min = p.t["ram_pressure_min_share"]
    out = []
    for d in view.device_ids():
        h = view.history(d)
        hot = [s for s in h if s.mem_used_pct >= thr]
        share = len(hot) / len(h)
        if share < share_min:
            continue
        last = h[-1]
        out.append(Finding(
            finding_id=f"ram_pressure/{d}", kind="ram_pressure", device_id=d, employee_id=last.employee_id,
            severity="high" if share >= 0.95 else "medium",
            title=f"Consistently RAM-constrained {last.model_name} ({d}, {last.ram_gb:.0f} GB)",
            explanation=(f"Memory use was at or above {thr}% in {len(hot)} of {len(h)} snapshots ({share:.0%}), "
                         f"averaging {mean(s.mem_used_pct for s in h):.1f}% on {last.ram_gb:.0f} GB."),
            metrics={"share_snapshots_constrained": round(share, 2), "constrained_snapshots": len(hot),
                     "total_snapshots": len(h), "mean_used_pct": round(mean(s.mem_used_pct for s in h), 1),
                     "max_used_pct": round(max(s.mem_used_pct for s in h), 1), "ram_gb": round(last.ram_gb)},
            evidence=_sample(hot), policy={"ram_pressure_used_pct": thr, "ram_pressure_min_share": share_min}))
    return out


# ---------------------------------------------------------------- battery
def detect_battery_eol(view: TenantView, p: Policy) -> list[Finding]:
    rated = p.t["battery_rated_cycles"]
    approach = p.t["battery_approaching_share"]
    fcc_thr = p.t["battery_fcc_decline_pct"]
    out = []
    for d in view.device_ids():
        h = [s for s in view.history(d) if s.battery is not None]
        if not h:
            continue
        first, last = h[0], h[-1]
        b = last.battery
        decline = 100.0 * (first.battery.full_charge_capacity - b.full_charge_capacity) / first.battery.full_charge_capacity
        service = [s for s in h if s.battery.condition != "Normal"]
        reasons = []
        if b.condition != "Normal":
            reasons.append(f"condition reported as '{b.condition}' since {service[0].day}")
        if b.cycle_count >= approach * rated:
            reasons.append(f"{b.cycle_count} charge cycles ({b.cycle_count / rated:.0%} of the {rated}-cycle rating)")
        if decline >= fcc_thr:
            reasons.append(f"full-charge capacity fell {decline:.1f}% in {len(h)} snapshots ({first.battery.full_charge_capacity} to {b.full_charge_capacity} mAh)")
        # Require at least two independent signals, or the OS's own service flag. One noisy signal is not enough.
        if not (b.condition != "Normal" or len(reasons) >= 2):
            continue
        ev = [first.snapshot_id, last.snapshot_id]
        if service:
            ev.insert(1, service[0].snapshot_id)
        out.append(Finding(
            finding_id=f"battery_eol/{d}", kind="battery_eol", device_id=d, employee_id=last.employee_id,
            severity="high" if len(reasons) >= 2 else "medium",
            title=f"Battery approaching end-of-life on {last.model_name} ({d})",
            explanation=_sentence("; ".join(reasons)),
            metrics={"cycle_count": b.cycle_count, "rated_cycles": rated, "condition": b.condition,
                     "fcc_first_mah": first.battery.full_charge_capacity, "fcc_last_mah": b.full_charge_capacity,
                     "fcc_decline_pct": round(decline, 1), "service_recommended_since": service[0].day if service else None,
                     "signals": len(reasons)},
            evidence=tuple(dict.fromkeys(ev)),
            policy={"battery_rated_cycles": rated, "battery_approaching_share": approach, "battery_fcc_decline_pct": fcc_thr}))
    return out


# ---------------------------------------------------------------- compliance
def detect_compliance_drift(view: TenantView, p: Policy) -> list[Finding]:
    """A check that passed earlier in the window and fails now. Checks that failed on
    every snapshot are reported by the compliance tools, not as drift."""
    out = []
    for d in view.device_ids():
        h = view.history(d)
        checks = {c.check_id for s in h for c in s.compliance}
        for cid in sorted(checks):
            seq = [(s, s.check(cid)) for s in h if s.check(cid)]
            if not seq or seq[-1][1].status != "fail":
                continue
            passes = [s for s, c in seq if c.status == "pass"]
            if not passes:
                continue
            # first failure after the last pass = start of the current failing streak
            last_pass = passes[-1]
            streak = [s for s, c in seq if s.collected_at > last_pass.collected_at]
            sev = seq[-1][1].severity
            out.append(Finding(
                finding_id=f"compliance_drift/{d}/{cid}", kind="compliance_drift", device_id=d,
                employee_id=h[-1].employee_id, severity=sev,
                title=f"{cid} drifted from pass to fail on {d}",
                explanation=(f"{cid} ({sev} severity) passed until {last_pass.day} and has failed on every snapshot since "
                             f"{streak[0].day} ({len(streak)} consecutive snapshots)."),
                metrics={"check_id": cid, "check_severity": sev, "last_pass": last_pass.day,
                         "first_fail": streak[0].day, "consecutive_fail_snapshots": len(streak)},
                evidence=(last_pass.snapshot_id, streak[0].snapshot_id, h[-1].snapshot_id)))
    return out


def compliance_drift_fleet(view: TenantView) -> dict[str, Any]:
    """Tenant-level daily count of failing devices per check, for trend questions."""
    days: dict[str, dict[str, int]] = {}
    for d in view.device_ids():
        for s in view.history(d):
            for c in s.compliance:
                days.setdefault(s.day, {}).setdefault(c.check_id, 0)
                if c.status == "fail":
                    days[s.day][c.check_id] += 1
    ordered = sorted(days)
    checks = sorted({c for v in days.values() for c in v})
    return {
        "devices": len(view.device_ids()),
        "series": {c: [{"day": day, "failing_devices": days[day].get(c, 0)} for day in ordered] for c in checks},
        "first_day": {c: days[ordered[0]].get(c, 0) for c in checks},
        "last_day": {c: days[ordered[-1]].get(c, 0) for c in checks},
        "window": [ordered[0], ordered[-1]],
    }


# ---------------------------------------------------------------- software
def detect_risky_software(view: TenantView, p: Policy) -> list[Finding]:
    risky = p.risky_software
    out = []
    for d in view.device_ids():
        h = view.history(d)
        for name, meta in risky.items():
            seen = [s for s in h if any(n == name for n, _ in s.software)]
            if not seen or seen[-1] is not h[-1]:
                continue  # only report software still installed on the latest snapshot
            before = [s for s in h if s.collected_at < seen[0].collected_at]
            appeared = f"first seen {seen[0].day}" + (f" (absent on {before[-1].day})" if before else " (present for the whole window)")
            ev = ([before[-1].snapshot_id] if before else []) + [seen[0].snapshot_id, h[-1].snapshot_id]
            out.append(Finding(
                finding_id=f"risky_software/{d}/{name.replace(' ', '_')}", kind="risky_software", device_id=d,
                employee_id=h[-1].employee_id, severity=meta["severity"],
                title=f"{name} installed on {d}",
                explanation=f"{name} ({meta['category']}) {appeared}. {meta['reason']}",
                metrics={"software": name, "category": meta["category"], "first_seen": seen[0].day,
                         "installed_during_window": bool(before)},
                evidence=tuple(dict.fromkeys(ev))))
    return out


# ---------------------------------------------------------------- data quality
def detect_telemetry_gaps(view: TenantView, p: Policy) -> list[Finding]:
    """Surfacing gaps stops the agent concluding 'battery removed' from a missing field."""
    out = []
    for d in view.device_ids():
        h = view.history(d)
        miss_b = [s for s in h if s.battery_missing_on_laptop]
        miss_n = [s for s in h if not s.has_network_block]
        if not miss_b and not miss_n:
            continue
        parts = []
        if miss_b:
            parts.append(f"battery block missing on {len(miss_b)} of {len(h)} snapshots of a laptop")
        if miss_n:
            parts.append(f"network block missing on {len(miss_n)} of {len(h)} snapshots")
        out.append(Finding(
            finding_id=f"telemetry_gap/{d}", kind="telemetry_gap", device_id=d, employee_id=h[-1].employee_id,
            severity="info", title=f"Incomplete telemetry from {d}",
            explanation=_sentence("; ".join(parts)) + " Treat these as collection gaps, not hardware changes.",
            metrics={"battery_missing_snapshots": len(miss_b), "network_missing_snapshots": len(miss_n), "total": len(h)},
            evidence=tuple(s.snapshot_id for s in (miss_b + miss_n)[:4])))
    return out


DETECTORS: dict[str, Callable[[TenantView, Policy], list[Finding]]] = {
    "disk_low": detect_disk_low,
    "disk_trend": detect_disk_trend,
    "ram_pressure": detect_ram_pressure,
    "battery_eol": detect_battery_eol,
    "compliance_drift": detect_compliance_drift,
    "risky_software": detect_risky_software,
    "telemetry_gap": detect_telemetry_gaps,
}


def run_detectors(view: TenantView, p: Policy, kinds: list[str] | None = None) -> list[Finding]:
    kinds = kinds or list(DETECTORS)
    unknown = set(kinds) - set(DETECTORS)
    if unknown:
        raise ValueError(f"unknown insight kinds {sorted(unknown)}; valid: {sorted(DETECTORS)}")
    found = [f for k in kinds for f in DETECTORS[k](view, p)]
    return sorted(found, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.kind, f.device_id))
