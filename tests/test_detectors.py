"""Detectors must agree with the independent ground truth (evals/build_ground_truth.py)."""
import pytest

from fleet_copilot.analytics.detectors import run_detectors

TENANTS = ["acme-001", "globex-002", "initech-003"]


def _devices(store, policy, tenant, kind):
    return sorted(f.device_id for f in run_detectors(store.for_tenant(tenant), policy, [kind]))


@pytest.mark.parametrize("tenant", TENANTS)
def test_disk_low_matches_ground_truth(store, policy, gt, tenant):
    assert _devices(store, policy, tenant, "disk_low") == gt[tenant]["low_disk"]


@pytest.mark.parametrize("tenant", TENANTS)
def test_ram_pressure_matches_ground_truth(store, policy, gt, tenant):
    assert _devices(store, policy, tenant, "ram_pressure") == gt[tenant]["ram_constrained"]


@pytest.mark.parametrize("tenant", TENANTS)
def test_battery_eol_matches_ground_truth(store, policy, gt, tenant):
    assert _devices(store, policy, tenant, "battery_eol") == gt[tenant]["battery_service_recommended"]


@pytest.mark.parametrize("tenant", TENANTS)
def test_screen_lock_drift_matches_ground_truth(store, policy, gt, tenant):
    found = sorted(f.device_id for f in run_detectors(store.for_tenant(tenant), policy, ["compliance_drift"])
                   if f.metrics["check_id"] == "screen_lock")
    assert found == gt[tenant]["screen_lock_drift"]


def test_every_finding_cites_real_snapshots(store, policy):
    for t in TENANTS:
        view = store.for_tenant(t)
        for f in run_detectors(view, policy):
            assert f.evidence, f.finding_id
            for sid in f.evidence:
                assert view.has_snapshot(sid), (f.finding_id, sid)
                assert sid.startswith(f.device_id + "/")


def test_disk_trend_projects_days_to_full(store, policy):
    f = {x.device_id: x for x in run_detectors(store.for_tenant("acme-001"), policy, ["disk_trend"])}
    assert "8NM23J95R5I6" in f
    m = f["8NM23J95R5I6"].metrics
    assert m["slope_gb_per_day"] < 0 and 0 < m["days_to_full"] < 30


def test_missing_battery_on_laptop_is_a_gap_not_a_finding(store, policy):
    gaps = {f.device_id for f in run_detectors(store.for_tenant("acme-001"), policy, ["telemetry_gap"])}
    assert "MT7PJB7N5LRE" in gaps  # MacBook Pro with 5 snapshots missing the battery block
    batt = {f.device_id for f in run_detectors(store.for_tenant("acme-001"), policy, ["battery_eol"])}
    assert "MT7PJB7N5LRE" not in batt


def test_risky_software_reports_install_date(store, policy):
    f = {x.finding_id: x for x in run_detectors(store.for_tenant("acme-001"), policy, ["risky_software"])}
    u = f["risky_software/1LYSSFD074BB/uTorrent"]
    assert u.metrics["first_seen"] == "2026-05-31" and u.metrics["installed_during_window"]
