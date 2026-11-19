"""Mutation test for the grounding verifier.

In the live eval run every first draft passed verification, which shows the agent grounds its
answers but says nothing about whether the verifier would catch a bad one. This script takes
the real drafts from evals/results/latest.json, breaks each one in a specific way, and checks
that the verifier rejects the result with the right issue code.

    uv run python evals/run_evals.py           # produces the traces (replay, offline)
    uv run python evals/verifier_mutations.py  # writes evals/results/mutations.md

Exits non-zero when an unmutated draft fails verification or a mutation goes undetected.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fleet_copilot.agent.verifier import CITE_RE, NUM_RE, _supported, extract_citations, verify  # noqa: E402

GT = json.loads((ROOT / "evals" / "ground_truth.json").read_text())
DATE_AT_END = re.compile(r"/\d{4}-\d{2}-\d{2}$")


def context(tenant: str, rec: dict) -> dict:
    foreign = [t for t in GT if t != tenant]
    return dict(ledger={i: {} for i in rec["ledger_ids"]}, numbers=set(rec["numbers"]),
                tenant_devices=GT[tenant]["devices"], tenant_employees=GT[tenant]["employees"],
                foreign_devices=[d for t in foreign for d in GT[t]["devices"]],
                foreign_employees=[e for t in foreign for e in GT[t]["employees"]],
                tools_used=len(rec["tool_calls"]))


def _outside_citations(text: str, pos: int) -> bool:
    return text.rfind("[", 0, pos) <= text.rfind("]", 0, pos)


# Each mutation returns the broken draft, or None when it does not apply to this draft.
def fabricated_citation(draft, rec, tenant):
    cites = extract_citations(draft)
    if not cites:
        return None
    c = cites[0]
    fake = DATE_AT_END.sub("/2026-01-01", c) if DATE_AT_END.search(c) else c.replace("/", "_unverified/", 1)
    return draft.replace(c, fake, 1)


def altered_number(draft, rec, tenant):
    numbers = set(rec["numbers"])
    for m in NUM_RE.finditer(draft):
        val = m.group(1)
        if float(val) <= 3 or not _outside_citations(draft, m.start()):
            continue
        new = f"{float(val) * 1.37 + 0.7:.1f}"
        if not _supported(new, numbers):
            return draft[:m.start(1)] + new + draft[m.end(1):]
    return None


def foreign_device(draft, rec, tenant):
    other = next(t for t in GT if t != tenant)
    cite = (extract_citations(draft) or ["x/y"])[0]
    return draft + f"\n\nDevice {GT[other]['devices'][0]} shows the same problem [{cite}]."


def execution_claim(draft, rec, tenant):
    return draft + "\n\nDone. I have opened a remediation ticket and the device owner has been notified."


def citations_removed(draft, rec, tenant):
    stripped = CITE_RE.sub("", draft)
    return stripped if stripped != draft else None


def uncited_device(draft, rec, tenant):
    unused = [d for d in GT[tenant]["devices"] if d not in draft]
    return draft + f"\n\nDevice {unused[0]} is also close to its limit." if unused else None


def swapped_numbers(draft, rec, tenant):
    """Swap two real numbers. Each is still in tool output, so this is the known blind spot."""
    spans = [m for m in NUM_RE.finditer(draft)
             if float(m.group(1)) > 3 and _outside_citations(draft, m.start())]
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            if a.group(1) != b.group(1):
                x, y = a.span(1), b.span(1)
                return draft[:x[0]] + b.group(1) + draft[x[1]:y[0]] + a.group(1) + draft[y[1]:]
    return None


# Mutations the verifier is not designed to catch. Measured and reported, never gated on.
BLIND_SPOTS = {"swapped_numbers": swapped_numbers}

MUTATIONS = {
    "fabricated_citation": (fabricated_citation, {"unretrieved_citation"}),
    "altered_number": (altered_number, {"unsupported_number"}),
    "foreign_device": (foreign_device, {"cross_tenant_leak"}),
    "execution_claim": (execution_claim, {"claims_execution"}),
    "citations_removed": (citations_removed, {"no_citations", "uncited_device"}),
    "uncited_device": (uncited_device, {"uncited_device"}),
}


def main() -> int:
    run = json.loads((ROOT / "evals" / "results" / "latest.json").read_text())[0]
    recs = [r for r in run if r.get("draft") and not r["refused"]]
    if not recs:
        print("No drafts in evals/results/latest.json. Run evals/run_evals.py first.")
        return 1
    baseline_fp = [r["id"] for r in recs if not verify(r["draft"], **context(r["tenant"], r)).ok]
    rows, misses = [], []
    for name, (fn, expected) in MUTATIONS.items():
        applied = caught = exact = 0
        for r in recs:
            broken = fn(r["draft"], r, r["tenant"])
            if broken is None:
                continue
            applied += 1
            v = verify(broken, **context(r["tenant"], r))
            caught += not v.ok
            hit = bool(expected & {i["code"] for i in v.issues})
            exact += hit
            if not hit:
                misses.append(f"{name} on {r['id']}: issues={[i['code'] for i in v.issues]}")
        rows.append((name, applied, caught, exact, ", ".join(sorted(expected))))
    total_applied = sum(r[1] for r in rows)
    total_exact = sum(r[3] for r in rows)
    lines = ["# Verifier mutation test", "",
             f"Drafts: {len(recs)} real agent answers from the recorded eval run.", "",
             f"Unmutated drafts rejected (false positives): **{len(baseline_fp)}/{len(recs)}**", "",
             "| Mutation | Applied | Rejected | Rejected with expected issue | Expected issue |", "|---|---|---|---|---|"]
    for name, applied, caught, exact, exp in rows:
        lines.append(f"| `{name}` | {applied} | {caught}/{applied} | {exact}/{applied} | {exp} |")
    lines += ["", f"Overall detection with the expected issue: **{total_exact}/{total_applied}**"]
    lines += ["", "## Known blind spot", "",
              "Swapping two numbers that both appear in tool output (a real value attached to the wrong device) is "
              "outside what a string-level verifier can see. Measured here so the limitation has a number:", "",
              "| Mutation | Applied | Rejected |", "|---|---|---|"]
    for name, fn in BLIND_SPOTS.items():
        outs = [fn(r["draft"], r, r["tenant"]) for r in recs]
        outs = [(o, r) for o, r in zip(outs, recs) if o is not None]
        rej = sum(not verify(o, **context(r["tenant"], r)).ok for o, r in outs)
        lines.append(f"| `{name}` | {len(outs)} | {rej}/{len(outs)} |")
    if misses:
        lines += ["", "Missed:", ""] + [f"- {m}" for m in misses]
    md = "\n".join(lines) + "\n"
    (ROOT / "evals" / "results" / "mutations.md").write_text(md)
    print(md)
    return 0 if not baseline_fp and not misses else 1


if __name__ == "__main__":
    sys.exit(main())
