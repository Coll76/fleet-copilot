"""Action guardrails: evidence, physical sense, tenant, role, privacy."""
import json

from fleet_copilot.tools.registry import dispatch


def run(ctx, name, args):
    return json.loads(dispatch(ctx, name, args)[0])


def insights(c, **kw):
    run(c, "run_insights", kw)


def test_action_needs_evidence_retrieved_this_turn(ctx):
    c = ctx()
    out = run(c, "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB",
                                          "justification": "x", "evidence": ["ram_pressure/M4XVHUV1MEPZ"]})
    assert out["status"] == "blocked_by_policy" and "not returned" in out["reason"]


def test_ram_upgrade_allowed_on_dell_with_finding(ctx):
    c = ctx()
    insights(c, kinds=["ram_pressure"])
    out = run(c, "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB",
                                          "justification": "x", "evidence": ["ram_pressure/M4XVHUV1MEPZ"]})
    assert out["status"] == "pending_approval"
    assert out["policy_facts"]["current_gb"] == 16 and out["policy_facts"]["requested_gb"] == 32
    assert c.proposals[-1]["status"] == "pending_approval"


def test_ram_upgrade_blocked_on_soldered_ram(ctx):
    c = ctx("initech-003")
    insights(c, kinds=["ram_pressure"])
    out = run(c, "create_upgrade_order", {"device_id": "I3JLKILKQTAC", "component": "ram", "spec": "16 GB",
                                          "justification": "x", "evidence": ["ram_pressure/I3JLKILKQTAC"]})
    assert out["status"] == "blocked_by_policy" and "not field-upgradeable" in out["reason"]


def test_upgrade_must_be_bigger_and_orderable(ctx):
    c = ctx()
    insights(c, kinds=["ram_pressure"])
    base = {"device_id": "M4XVHUV1MEPZ", "component": "ram", "justification": "x", "evidence": ["ram_pressure/M4XVHUV1MEPZ"]}
    assert "not larger" in run(c, "create_upgrade_order", {**base, "spec": "16 GB"})["reason"]
    assert "orderable" in run(c, "create_upgrade_order", {**base, "spec": "24 GB"})["reason"]


def test_upgrade_needs_matching_finding_kind(ctx):
    c = ctx()
    insights(c, kinds=["battery_eol"])
    out = run(c, "create_upgrade_order", {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB",
                                          "justification": "x", "evidence": ["battery_eol/M4XVHUV1MEPZ"]})
    assert out["status"] == "blocked_by_policy" and "ram_pressure" in out["reason"]


def test_replacement_needs_two_dimensions(ctx):
    c = ctx()
    insights(c)
    one = run(c, "flag_device_for_replacement", {"device_id": "7VP16KHV88LM", "reason": "battery",
                                                 "evidence": ["battery_eol/7VP16KHV88LM"]})
    assert one["status"] == "blocked_by_policy"
    two = run(c, "flag_device_for_replacement", {"device_id": "8NM23J95R5I6", "reason": "battery and disk",
                                                 "evidence": ["battery_eol/8NM23J95R5I6", "disk_low/8NM23J95R5I6"]})
    assert two["status"] == "pending_approval"


def test_replacement_allowed_for_chronic_ram_on_soldered_device(ctx):
    c = ctx("initech-003")
    insights(c, kinds=["ram_pressure"])
    out = run(c, "flag_device_for_replacement", {"device_id": "I3JLKILKQTAC", "reason": "8 GB soldered RAM",
                                                 "evidence": ["ram_pressure/I3JLKILKQTAC"]})
    assert out["status"] == "pending_approval" and "cannot be upgraded" in out["policy_facts"]["basis"]


def test_ticket_for_passing_check_is_refused(ctx):
    c = ctx()
    out_ = run(c, "get_device", {"device_id": "Q3VUITEGQ400"})
    out = run(c, "open_remediation_ticket", {"device_id": "Q3VUITEGQ400", "check_id": "screen_lock", "note": "fix",
                                             "evidence": [out_["snapshot_id"]]})
    assert out["status"] == "blocked_by_policy" and "passing" in out["reason"]


def test_ticket_for_failing_check_allowed(ctx):
    c = ctx()
    snap = run(c, "get_device", {"device_id": "1LYSSFD074BB"})["snapshot_id"]
    out = run(c, "open_remediation_ticket", {"device_id": "1LYSSFD074BB", "check_id": "screen_lock",
                                             "note": "Re-enable screen lock via MDM.", "evidence": [snap]})
    assert out["status"] == "pending_approval"


def test_software_ticket(ctx):
    c = ctx()
    insights(c, kinds=["risky_software"])
    out = run(c, "open_remediation_ticket", {"device_id": "1LYSSFD074BB", "check_id": "software:uTorrent",
                                             "note": "Remove uTorrent.", "evidence": ["risky_software/1LYSSFD074BB/uTorrent"]})
    assert out["status"] == "pending_approval"


def test_notify_other_tenant_employee_refused(ctx):
    c = ctx()
    insights(c, kinds=["disk_low"])
    out = run(c, "notify_employee", {"employee_id": "emp-globex-1002", "message": "hi", "evidence": ["disk_low/8NM23J95R5I6"]})
    assert out["status"] == "blocked_by_policy" and "not found" in out["reason"]


def test_notify_cannot_leak_other_devices(ctx):
    c = ctx()
    insights(c, kinds=["disk_low"])
    out = run(c, "notify_employee", {"employee_id": "emp-acme-1005",
                                     "message": "Your laptop 8NM23J95R5I6 is nearly full, like MT7PJB7N5LRE.",
                                     "evidence": ["disk_low/8NM23J95R5I6"]})
    assert out["status"] == "blocked_by_policy" and "MT7PJB7N5LRE" in out["reason"]


def test_viewer_cannot_propose(ctx):
    c = ctx(role="viewer")
    text, err = dispatch(c, "notify_employee", {"employee_id": "emp-acme-1005", "message": "x", "evidence": ["a/b"]})
    assert err and "cannot propose" in text


def test_duplicate_proposals_collapse(ctx):
    c = ctx()
    insights(c, kinds=["ram_pressure"])
    args = {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB", "justification": "x",
            "evidence": ["ram_pressure/M4XVHUV1MEPZ"]}
    first = run(c, "create_upgrade_order", args)
    second = run(c, "create_upgrade_order", {**args, "justification": "y"})
    assert second["status"] == "duplicate" and second["proposal_id"] == first["proposal_id"]
