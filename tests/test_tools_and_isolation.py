"""Read tools return grounded facts, and tenant isolation holds at the data layer."""
import json

import pytest

from fleet_copilot.data.store import NotFound
from fleet_copilot.tools.registry import dispatch, tool_specs


def run(ctx, name, args):
    text, err = dispatch(ctx, name, args)
    return json.loads(text), err


def test_no_tool_accepts_a_tenant_parameter():
    for spec in tool_specs():
        props = spec["input_schema"].get("properties", {})
        assert not any("company" in p or "tenant" in p for p in props), spec["name"]


def test_tenant_view_cannot_reach_other_tenant(store):
    acme = store.for_tenant("acme-001")
    globex_dev = store.for_tenant("globex-002").device_ids()[0]
    with pytest.raises(NotFound):
        acme.latest(globex_dev)
    assert globex_dev not in acme.device_ids()


def test_foreign_device_error_matches_unknown_device(ctx, store):
    c = ctx("acme-001")
    foreign = store.for_tenant("globex-002").device_ids()[0]
    a, err_a = run(c, "get_device", {"device_id": foreign})
    b, err_b = run(c, "get_device", {"device_id": "DOESNOTEXIST"})
    assert err_a and err_b
    assert a["error"].replace(foreign, "X") == b["error"].replace("DOESNOTEXIST", "X")  # no existence oracle


def test_low_disk_query(ctx, gt):
    out, err = run(ctx("acme-001"), "query_devices", {"disk_free_pct_below": 10})
    assert not err
    assert sorted(m["device_id"] for m in out["matches"]) == gt["acme-001"]["low_disk"]
    assert all(m["snapshot_id"] == gt["acme-001"]["latest_snapshot_ids"][m["device_id"]] for m in out["matches"])


def test_high_severity_laptops_is_empty_and_says_what_does_fail(ctx):
    out, _ = run(ctx("acme-001"), "query_devices", {"device_class": "laptop", "failing_severity": "high"})
    assert out["match_count"] == 0
    assert out["tenant_failing_checks_by_severity"].get("medium", 0) > 0


def test_os_version_comparison_requires_os_name(ctx):
    out, err = run(ctx("acme-001"), "query_devices", {"os_version_below": "15"})
    assert err and "os_name" in out["error"]


def test_old_macos(ctx, gt):
    out, _ = run(ctx("initech-003"), "query_devices", {"os_name": "macOS", "os_version_below": "15"})
    assert sorted(m["device_id"] for m in out["matches"]) == gt["initech-003"]["old_macos_lt_15"]


def test_tools_fill_the_evidence_ledger(ctx):
    c = ctx("acme-001")
    run(c, "run_insights", {"kinds": ["disk_low"]})
    assert "disk_low/8NM23J95R5I6" in c.ledger
    assert "8NM23J95R5I6/2026-06-12" in c.ledger


def test_unknown_tool_and_bad_args_are_errors_not_crashes(ctx):
    c = ctx()
    assert run(c, "delete_everything", {})[1]
    assert run(c, "query_devices", {"disk_free_pct_below": "lots"})[1]
    assert run(c, "query_devices", {"company_id": "globex-002"})[1]  # extra fields are forbidden


def test_compliance_overview_trend(ctx, gt):
    out, _ = run(ctx("acme-001"), "compliance_overview", {"check_id": "screen_lock"})
    tr = out["trend_over_window"]["screen_lock"]
    assert tr["failing_devices_first_day"] == gt["acme-001"]["screen_lock_failing_first_day"]
    assert tr["failing_devices_last_day"] == len(gt["acme-001"]["screen_lock_failing_now"])
