"""Deterministic grounding verifier.

Runs on every final answer before the admin sees it. It does not ask an LLM whether
the answer "looks grounded"; it checks mechanically:

  unretrieved_citation  a cited id was not returned by any tool this turn
  uncited_device        a device the answer talks about has no citation for it
  cross_tenant_leak     the answer names a device or employee from another tenant (hard block)
  unsupported_number    a number in the answer does not appear in any tool output
  claims_execution      the answer says an action was done, but actions only run after approval
  no_citations          the answer makes fleet claims without citing anything

On failure the agent gets one repair attempt with the list of problems. If it still
fails, the answer is shown with a visible warning (or replaced, for a leak).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

CITE_RE = re.compile(r"\[([^\[\]\n]{3,200})\]")
ID_RE = re.compile(r"[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]+")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:T[\d:]+Z?)?\b")
NUM_RE = re.compile(r"(?<![\w./-])(\d+(?:\.\d+)?)(?![\w/-])")
EXEC_RE = re.compile(
    r"\b(?:I|we)\s+(?:have\s+|'ve\s+)?(?:created|opened|placed|ordered|flagged|sent|notified|submitted|executed|filed)\b"
    r"|\b(?:has|have)\s+been\s+(?:created|opened|placed|ordered|flagged|sent|notified|submitted|executed|filed)\b",
    re.I)
# Words that make a matched execution phrase acceptable (talking about proposals, not execution).
EXEC_OK_RE = re.compile(r"propos|pending|await|approv|draft|once you|if you|would", re.I)


@dataclass
class Verification:
    ok: bool
    issues: list[dict[str, str]] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)
    devices_mentioned: list[str] = field(default_factory=list)
    hard_block: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "issues": self.issues, "citations": self.citations,
                "devices_mentioned": self.devices_mentioned, "hard_block": self.hard_block}


def extract_citations(text: str) -> list[str]:
    out: list[str] = []
    for group in CITE_RE.findall(text):
        for tok in re.split(r"[;,\s]+", group):
            tok = tok.strip().strip("`'\"")
            if ID_RE.fullmatch(tok):
                out.append(tok)
    return list(dict.fromkeys(out))


def strip_ids(text: str) -> str:
    """Remove citations, dates and identifiers so only real quantities remain."""
    stripped = CITE_RE.sub(" ", text)
    stripped = re.sub(r"[A-Za-z0-9_\-]+/[A-Za-z0-9_\-./]+", " ", stripped)  # snapshot and finding ids
    stripped = DATE_RE.sub(" ", stripped)
    stripped = re.sub(r"\b(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]{8,}\b", " ", stripped)  # device ids / serials
    stripped = re.sub(r"emp-[a-z]+-\d+|\b[a-z]+-\d{3}\b|\b[PA]-[0-9a-f]{8,10}\b|\b(?:Mac|XPS-)\d+,?\d*", " ", stripped)
    return stripped


def _numbers_in(text: str) -> list[str]:
    return NUM_RE.findall(strip_ids(text))


def _supported(num: str, known: set[str]) -> bool:
    f = float(num)
    if f.is_integer() and f <= 3:
        return True  # small counts and ordinals ("one of", "two devices", "step 1") are not worth flagging
    if (str(int(f)) if f.is_integer() else f"{f:.1f}") in known:
        return True
    for k in known:
        try:
            if abs(float(k) - f) <= 0.051 or (f > 10 and abs(float(k) - f) <= 0.5):
                return True
        except ValueError:
            continue
    return False


def verify(answer: str, *, ledger: dict[str, Any], numbers: set[str], tenant_devices: list[str],
           tenant_employees: list[str], foreign_devices: list[str], foreign_employees: list[str],
           tools_used: int, executed_ids: list[str] | None = None) -> Verification:
    issues: list[dict[str, str]] = []
    cites = extract_citations(answer)
    hard = False

    for c in cites:
        if c not in ledger:
            issues.append({"code": "unretrieved_citation", "detail": f"[{c}] was not returned by any tool in this turn."})

    leaked = [d for d in foreign_devices if d in answer] + [e for e in foreign_employees if e in answer]
    if leaked:
        hard = True
        issues.append({"code": "cross_tenant_leak", "detail": f"Answer mentions ids outside this tenant: {leaked}."})

    mentioned = [d for d in tenant_devices if d in CITE_RE.sub(" ", answer)]
    for d in mentioned:
        if not any(d in c for c in cites):
            issues.append({"code": "uncited_device", "detail": f"{d} is discussed but no citation refers to it."})

    bad_nums = sorted({n for n in _numbers_in(answer) if not _supported(n, numbers)}, key=float)
    if bad_nums:
        issues.append({"code": "unsupported_number",
                       "detail": f"Numbers not found in any tool output: {bad_nums[:8]}. Remove them or get them from a tool."})

    for m in EXEC_RE.finditer(answer):
        window = answer[max(0, m.start() - 60): m.end() + 60]
        if not EXEC_OK_RE.search(window) and not executed_ids:
            issues.append({"code": "claims_execution",
                           "detail": f"'{m.group(0)}' implies an action ran. Actions only run after human approval; say 'proposed'."})
            break

    if tools_used and not cites and (mentioned or _numbers_in(answer)):
        issues.append({"code": "no_citations", "detail": "The answer states fleet facts but cites no evidence ids."})

    return Verification(ok=not issues, issues=issues, citations=cites, devices_mentioned=mentioned, hard_block=hard)
