"""Telemetry store with tenant isolation built into the access path.

The agent never receives the global store. It receives a TenantView, which only
holds one company's snapshots. A device id from another company raises the same
NotFound error as an id that does not exist, so the agent cannot even learn that
another tenant's device exists (no existence oracle).
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from .models import Snapshot, snapshot_from_raw

DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data" / "telemetry.jsonl"


class NotFound(LookupError):
    """Raised for unknown ids and for ids that belong to another tenant."""


class TelemetryStore:
    def __init__(self, snapshots: list[Snapshot]):
        self._by_tenant: dict[str, list[Snapshot]] = defaultdict(list)
        for s in snapshots:
            self._by_tenant[s.company_id].append(s)

    @classmethod
    def from_jsonl(cls, path: Path | str = DEFAULT_DATA) -> "TelemetryStore":
        with open(path) as fh:
            snaps = [snapshot_from_raw(json.loads(line)) for line in fh if line.strip()]
        return cls(snaps)

    @property
    def tenants(self) -> list[str]:
        return sorted(self._by_tenant)

    def for_tenant(self, company_id: str) -> "TenantView":
        if company_id not in self._by_tenant:
            raise NotFound(f"unknown tenant {company_id!r}")
        return TenantView(company_id, tuple(self._by_tenant[company_id]))


@dataclass(frozen=True)
class TenantView:
    company_id: str
    _snapshots: tuple[Snapshot, ...]

    @cached_property
    def _by_device(self) -> dict[str, list[Snapshot]]:
        out: dict[str, list[Snapshot]] = defaultdict(list)
        for s in self._snapshots:
            out[s.device_id].append(s)
        for v in out.values():
            v.sort(key=lambda s: s.collected_at)
        return dict(out)

    @cached_property
    def _by_id(self) -> dict[str, Snapshot]:
        return {s.snapshot_id: s for s in self._snapshots}

    def device_ids(self) -> list[str]:
        return sorted(self._by_device)

    def employee_ids(self) -> list[str]:
        return sorted({s.employee_id for s in self._snapshots})

    def owns_device(self, device_id: str) -> bool:
        return device_id in self._by_device

    def owns_employee(self, employee_id: str) -> bool:
        return employee_id in set(self.employee_ids())

    def history(self, device_id: str) -> list[Snapshot]:
        try:
            return self._by_device[device_id]
        except KeyError:
            raise NotFound(f"device {device_id!r} not found") from None

    def latest(self, device_id: str) -> Snapshot:
        return self.history(device_id)[-1]

    def latest_all(self) -> list[Snapshot]:
        return [self.latest(d) for d in self.device_ids()]

    def snapshot(self, snapshot_id: str) -> Snapshot:
        try:
            return self._by_id[snapshot_id]
        except KeyError:
            raise NotFound(f"snapshot {snapshot_id!r} not found") from None

    def has_snapshot(self, snapshot_id: str) -> bool:
        return snapshot_id in self._by_id

    @property
    def window(self) -> tuple[str, str]:
        days = sorted(s.day for s in self._snapshots)
        return days[0], days[-1]
