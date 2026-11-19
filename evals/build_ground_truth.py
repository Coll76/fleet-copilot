"""Independent ground truth for the evaluation suite.

Deliberately does NOT import fleet_copilot. It re-derives the expected answers from
the raw JSONL with separate, simple code, so a bug in the package's detectors or
tools cannot silently make the evals agree with it. Thresholds are read from the
same policy file because they are definitions, not logic.

Run:  uv run python evals/build_ground_truth.py   (writes evals/ground_truth.json)
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
raw = [json.loads(line) for line in open(ROOT / "data" / "telemetry.jsonl")]
pol = yaml.safe_load(open(ROOT / "config" / "policy.yaml"))
T = pol["thresholds"]

by_dev: dict[str, list[dict]] = defaultdict(list)
for r in raw:
    by_dev[r["device_id"]].append(r)
for v in by_dev.values():
    v.sort(key=lambda r: r["collected_at"])

tenants = sorted({r["company_id"] for r in raw})
gt: dict[str, dict] = {}
for t in tenants:
    devs = sorted(d for d, rs in by_dev.items() if rs[0]["company_id"] == t)
    latest = {d: by_dev[d][-1] for d in devs}

    def free_pct(r):
        v = r["disk_volumes"][0]
        return 100 * v["available_bytes"] / v["size_bytes"]

    def used_pct(r):
        m = r["memory"]
        return 100 * m["used_memory_bytes"] / m["total_memory_bytes"]

    laptops = [d for d in devs if latest[d]["device_identity"]["model_name"] != "Mac mini"]
    macs = [d for d in devs if latest[d]["os"]["product_name"] == "macOS"]

    low_disk = [d for d in devs if free_pct(latest[d]) < T["disk_low_free_pct"]]
    old_macos = [d for d in macs if int(latest[d]["os"]["product_version"].split(".")[0]) < 15]
    high_sev_fail_laptops = [d for d in laptops if any(c["status"] == "fail" and c["severity"] == "high"
                                                       for c in latest[d]["compliance_results"])]
    ram = [d for d in devs if sum(used_pct(r) >= T["ram_pressure_used_pct"] for r in by_dev[d]) / len(by_dev[d])
           >= T["ram_pressure_min_share"]]
    battery = []
    for d in devs:
        bs = [r["battery"] for r in by_dev[d] if r.get("battery")]
        if bs and (bs[-1]["condition"] != "Normal"):
            battery.append(d)
    drift = []
    for d in devs:
        seq = [next((c["status"] for c in r["compliance_results"] if c["check_id"] == "screen_lock"), None) for r in by_dev[d]]
        if seq[-1] == "fail" and "pass" in seq:
            drift.append(d)
    screen_fail_now = [d for d in devs if any(c["check_id"] == "screen_lock" and c["status"] == "fail"
                                                 for c in latest[d]["compliance_results"])]
    utorrent = [d for d in devs if any(s["name"] == "uTorrent" for s in latest[d]["installed_software"])]
    gt[t] = {
        "devices": devs,
        "employees": sorted({r["employee_id"] for d in devs for r in by_dev[d]}),
        "low_disk": low_disk,
        "old_macos_lt_15": old_macos,
        "laptops_failing_high_severity": high_sev_fail_laptops,
        "ram_constrained": ram,
        "battery_service_recommended": battery,
        "screen_lock_drift": drift,
        "screen_lock_failing_now": screen_fail_now,
        "screen_lock_failing_first_day": sum(
            any(c["check_id"] == "screen_lock" and c["status"] == "fail" for c in by_dev[d][0]["compliance_results"]) for d in devs),
        "utorrent": utorrent,
        "latest_snapshot_ids": {d: f"{d}/{latest[d]['collected_at'][:10]}" for d in devs},
    }

out = ROOT / "evals" / "ground_truth.json"
out.write_text(json.dumps(gt, indent=1))
for t, g in gt.items():
    print(t, {k: (len(v) if isinstance(v, list) else v) for k, v in g.items() if k != "latest_snapshot_ids"})
