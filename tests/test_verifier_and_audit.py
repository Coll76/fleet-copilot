from fleet_copilot.agent.verifier import extract_citations, verify
from fleet_copilot.guardrails.audit import AuditLog

BASE = dict(tenant_devices=["AAA111BBB222", "CCC333DDD444"], tenant_employees=["emp-acme-1"],
            foreign_devices=["ZZZ999YYY888"], foreign_employees=["emp-globex-1"], tools_used=1)


def v(text, ledger=None, numbers=None, **kw):
    return verify(text, ledger=ledger or {}, numbers=set(numbers or []), **{**BASE, **kw})


def test_extracts_multiple_ids_from_one_bracket():
    assert extract_citations("x [a/1, disk_low/AAA111BBB222; AAA111BBB222/2026-06-12] y") == [
        "a/1", "disk_low/AAA111BBB222", "AAA111BBB222/2026-06-12"]


def test_grounded_answer_passes():
    r = v("AAA111BBB222 has 2.6% free [AAA111BBB222/2026-06-12].", ledger={"AAA111BBB222/2026-06-12": {}}, numbers={"2.6"})
    assert r.ok, r.issues


def test_invented_citation_fails():
    r = v("AAA111BBB222 is low [AAA111BBB222/2026-06-01].", ledger={"AAA111BBB222/2026-06-12": {}})
    assert [i["code"] for i in r.issues] == ["unretrieved_citation"]


def test_unsupported_number_fails():
    r = v("AAA111BBB222 will be full in 7 days [x/AAA111BBB222].", ledger={"x/AAA111BBB222": {}}, numbers={"5"})
    assert "unsupported_number" in [i["code"] for i in r.issues]


def test_cross_tenant_leak_is_hard_block():
    r = v("Also ZZZ999YYY888 is low.")
    assert r.hard_block


def test_claimed_execution_fails_but_proposal_language_passes():
    assert "claims_execution" in [i["code"] for i in v("I have created a ticket for it.").issues]
    assert "claims_execution" not in [i["code"] for i in v("I have created a proposal awaiting your approval.").issues]


def test_uncited_device_fails():
    r = v("CCC333DDD444 looks fine.")
    assert "uncited_device" in [i["code"] for i in r.issues]


def test_audit_chain_detects_tampering(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    for i in range(3):
        log.write("e", tenant_id="t", actor="u", i=i)
    assert log.verify_chain() == (True, None)
    lines = (tmp_path / "a.jsonl").read_text().splitlines()
    lines[1] = lines[1].replace('"i": 1', '"i": 7')
    (tmp_path / "a.jsonl").write_text("\n".join(lines) + "\n")
    assert log.verify_chain() == (False, 1)
