"""Per-turn tool context.

Holds the tenant-scoped view, the policy, and the evidence ledger. The ledger is
the backbone of grounding: every snapshot id and finding id a tool returns is
recorded here, and the verifier later accepts a citation only if it is in the
ledger for this turn. An id the model "remembers" or invents is rejected even if
it happens to exist in the dataset.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..analytics.detectors import Finding, run_detectors
from ..analytics.policy import Policy
from ..data.store import TenantView


@dataclass
class Session:
    tenant_id: str
    user_id: str
    role: str = "it_admin"  # "it_admin" may propose actions, "viewer" may only read


@dataclass
class ToolContext:
    session: Session
    view: TenantView
    policy: Policy
    ledger: dict[str, Any] = field(default_factory=dict)  # evidence id -> what was returned
    numbers: set[str] = field(default_factory=set)  # every number a tool emitted, for numeric grounding
    proposals: list[dict[str, Any]] = field(default_factory=list)
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    _findings: dict[str, Finding] | None = None

    # -------- evidence
    def record_snapshot(self, snapshot_id: str, payload: Any) -> None:
        self.ledger[snapshot_id] = payload

    def record_finding(self, f: Finding) -> None:
        self.ledger[f.finding_id] = f.to_dict()
        for sid in f.evidence:
            self.ledger.setdefault(sid, {"via_finding": f.finding_id})

    def findings(self) -> dict[str, Finding]:
        """All findings for this tenant, computed once per turn. Used by the action
        policy so that an action can be validated against detector output even if
        the model has not called run_insights itself."""
        if self._findings is None:
            self._findings = {f.finding_id: f for f in run_detectors(self.view, self.policy)}
        return self._findings
