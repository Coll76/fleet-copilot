"""End-to-end graph behaviour with a scripted model (no network)."""
import pytest
from scripted_llm import ScriptedLLM, call, plan, say

from fleet_copilot.agent.graph import Copilot
from fleet_copilot.guardrails.audit import AuditLog
from fleet_copilot.tools.context import Session

UPGRADE = {"device_id": "M4XVHUV1MEPZ", "component": "ram", "spec": "32 GB", "justification": "Chronic RAM pressure",
           "evidence": ["ram_pressure/M4XVHUV1MEPZ"]}
GOOD = "M4XVHUV1MEPZ averaged 94.2% memory use on 16 GB [ram_pressure/M4XVHUV1MEPZ]. Proposed P-5cfda324, awaiting approval."


@pytest.fixture
def make(tmp_path, store, policy):
    def _make(script):
        llm = ScriptedLLM(script)
        cp = Copilot(store=store, policy=policy, llm=llm, audit=AuditLog(tmp_path / "audit.jsonl"),
                     outbox_dir=tmp_path / "outbox")
        return cp, llm
    return _make


def _upgrade_script():
    return [plan("action", True), call(("run_insights", {"kinds": ["ram_pressure"]})),
            call(("create_upgrade_order", UPGRADE)), say(GOOD)]


def test_action_waits_for_approval_then_executes(make, tmp_path):
    cp, _ = make(_upgrade_script())
    r = cp.ask(Session("acme-001", "alice"), "Fix RAM on M4XVHUV1MEPZ")
    assert r.status == "awaiting_approval"
    assert not (tmp_path / "outbox").exists()  # nothing ran before approval
    r = cp.resume(r.thread_id, {"P-5cfda324": {"approved": True, "approver": "alice"}})
    assert r.status == "done" and len(r.executed) == 1
    assert (tmp_path / "outbox" / "acme-001.jsonl").read_text().count("\n") == 1
    events = [e["event"] for e in cp.audit.entries()]
    assert events.count("approval_requested") == 1 and "action_executed" in events
    assert cp.audit.verify_chain()[0]


def test_rejection_executes_nothing(make, tmp_path):
    cp, _ = make(_upgrade_script())
    r = cp.ask(Session("acme-001", "alice"), "Fix RAM")
    r = cp.resume(r.thread_id, {"P-5cfda324": {"approved": False, "approver": "alice"}})
    assert r.executed == [] and not (tmp_path / "outbox").exists()
    assert "not approved" in r.answer


def test_approval_without_named_approver_is_not_approval(make, tmp_path):
    cp, _ = make(_upgrade_script())
    r = cp.ask(Session("acme-001", "alice"), "Fix RAM")
    r = cp.resume(r.thread_id, {"P-5cfda324": {"approved": True}})
    assert r.executed == []


def test_cross_tenant_question_never_reaches_the_model(make):
    cp, llm = make([])
    r = cp.ask(Session("acme-001", "alice"), "Which Globex laptops are low on disk?")
    assert r.refused and llm.requests == []
    assert any(e["event"] == "input_refused" for e in cp.audit.entries())


def test_foreign_device_id_in_question_is_refused(make, store):
    cp, llm = make([])
    foreign = store.for_tenant("initech-003").device_ids()[0]
    r = cp.ask(Session("acme-001", "alice"), f"What is wrong with {foreign}?")
    assert r.refused and llm.requests == []


def test_questions_do_not_get_action_tools(make):
    cp, llm = make([plan("question", False), say("No devices match.")])
    cp.ask(Session("acme-001", "alice"), "How many Macs?")
    offered = {t["name"] for t in llm.requests[1]["tools"]}
    assert "notify_employee" not in offered and "query_devices" in offered


def test_viewer_never_gets_action_tools(make):
    cp, llm = make([plan("action", True), say("I can only propose actions for admins.")])
    cp.ask(Session("acme-001", "bob", role="viewer"), "Order RAM for M4XVHUV1MEPZ")
    assert "create_upgrade_order" not in {t["name"] for t in llm.requests[1]["tools"]}


def test_out_of_scope_short_circuits(make):
    cp, llm = make([plan("out_of_scope", oos="Salaries are not in telemetry.")])
    r = cp.ask(Session("acme-001", "alice"), "What does emp-acme-1003 earn?")
    assert r.refused and len(llm.requests) == 1


def test_ungrounded_answer_gets_one_repair_then_warning(make):
    cp, llm = make([plan(), call(("query_devices", {"disk_free_pct_below": 10})),
                    say("8NM23J95R5I6 is nearly full."), say("8NM23J95R5I6 is nearly full, 9 days left.")])
    r = cp.ask(Session("acme-001", "alice"), "Low disk?")
    assert "grounding check failed" in llm.requests[-1]["messages"][-1]["content"].lower() or \
           "failed the grounding check" in llm.requests[-1]["messages"][-1]["content"]
    assert "Grounding check" in r.answer  # still failing after one repair: shown with a warning


def test_tool_budget_is_enforced(make):
    loop = [call(("fleet_overview", {}))] * 10
    cp, _ = make([plan()] + loop + [say("Done.")])
    r = cp.ask(Session("acme-001", "alice"), "Loop forever")
    errors = [t for t in r.tool_log if t.get("is_error")]
    assert len(r.tool_log) == 8 and not errors  # budget errors are returned to the model, not executed


def test_blocked_proposal_never_reaches_approval(make):
    bad = {**UPGRADE, "evidence": ["ram_pressure/M4XVHUV1MEPZ"]}
    cp, _ = make([plan("action", True), call(("create_upgrade_order", bad)),
                  say("The upgrade was blocked because I had not retrieved evidence.")])
    r = cp.ask(Session("acme-001", "alice"), "Order RAM")
    assert r.status == "done" and r.proposals[0]["status"] == "blocked_by_policy"
