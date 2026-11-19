"""Normalised view of one telemetry snapshot.

Raw records are nested JSON from the desktop agent. Everything the agent reasons
about is derived here once, deterministically, so the LLM never does arithmetic on
raw bytes and every derived value can be traced back to a field in the source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

GIB = 1024**3

# Model names that are desktops. A laptop with no battery block is a telemetry gap,
# a desktop with no battery block is expected. Keeping this explicit avoids the agent
# guessing from missing fields.
DESKTOP_MODELS = {"Mac mini", "Mac Studio", "Mac Pro", "iMac"}


def parse_version(v: str) -> tuple[int, ...]:
    """'14.5' -> (14, 5). '10 22H2' -> (10,). Non-numeric parts are ignored."""
    head = v.split()[0]
    out = []
    for part in head.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)


@dataclass(frozen=True)
class Compliance:
    check_id: str
    status: str  # "pass" | "fail"
    severity: str  # "low" | "medium" | "high"


@dataclass(frozen=True)
class Battery:
    condition: str
    cycle_count: int
    full_charge_capacity: int
    percentage: int
    charging_status: str


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str  # "<device_id>/<YYYY-MM-DD>" stable, human-citable id
    device_id: str
    company_id: str
    employee_id: str
    collected_at: datetime
    hostname: str
    model_name: str
    device_class: str  # "laptop" | "desktop"
    os_name: str  # "macOS" | "Windows"
    os_version: str  # as reported, e.g. "14.5", "10 22H2"
    os_version_tuple: tuple[int, ...]
    ram_gb: float
    mem_used_pct: float
    disk_size_gb: float
    disk_free_gb: float
    disk_free_pct: float
    disk_encrypted: bool
    battery: Battery | None
    has_network_block: bool
    compliance: tuple[Compliance, ...]
    software: tuple[tuple[str, str], ...]  # (name, version)
    raw: dict[str, Any] = field(repr=False, compare=False, hash=False)

    @property
    def day(self) -> str:
        return self.collected_at.strftime("%Y-%m-%d")

    @property
    def battery_missing_on_laptop(self) -> bool:
        return self.device_class == "laptop" and self.battery is None

    def check(self, check_id: str) -> Compliance | None:
        return next((c for c in self.compliance if c.check_id == check_id), None)

    def summary(self) -> dict[str, Any]:
        """Compact, JSON-safe dict for tool outputs. Every value is a source field
        or a documented derivation of one."""
        b = self.battery
        return {
            "snapshot_id": self.snapshot_id,
            "device_id": self.device_id,
            "employee_id": self.employee_id,
            "collected_at": self.collected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hostname": self.hostname,
            "model": self.model_name,
            "device_class": self.device_class,
            "os": f"{self.os_name} {self.os_version}",
            "ram_gb": round(self.ram_gb),
            "mem_used_pct": round(self.mem_used_pct, 1),
            "disk_size_gb": round(self.disk_size_gb),
            "disk_free_gb": round(self.disk_free_gb, 1),
            "disk_free_pct": round(self.disk_free_pct, 1),
            "disk_encrypted": self.disk_encrypted,
            "battery": None
            if b is None
            else {
                "condition": b.condition,
                "cycle_count": b.cycle_count,
                "full_charge_capacity_mah": b.full_charge_capacity,
            },
            "battery_missing_on_laptop": self.battery_missing_on_laptop,
            "compliance": [
                {"check_id": c.check_id, "status": c.status, "severity": c.severity}
                for c in self.compliance
            ],
            "installed_software": [f"{n} {v}" for n, v in self.software],
        }


def snapshot_from_raw(r: dict[str, Any]) -> Snapshot:
    collected = datetime.strptime(r["collected_at"], "%Y-%m-%dT%H:%M:%SZ")
    os_ = r.get("os") or {}
    ident = r.get("device_identity") or {}
    mem = r.get("memory") or {}
    vols = r.get("disk_volumes") or []
    # The fleet has one volume per device. If more appear, the root volume is the
    # one that matters for "low on disk", so prefer mount_point "/" or "C:\".
    root = next((v for v in vols if v.get("mount_point") in ("/", "C:\\", "C:")), vols[0] if vols else None)
    total_mem = mem.get("total_memory_bytes") or mem.get("ram_bytes") or 0
    used_mem = mem.get("used_memory_bytes") or 0
    b = r.get("battery")
    battery = None
    if b and b.get("battery_present", True):
        battery = Battery(
            condition=b.get("condition", "Unknown"),
            cycle_count=int(b.get("cycle_count", 0)),
            full_charge_capacity=int(b.get("full_charge_capacity", 0)),
            percentage=int(b.get("percentage", 0)),
            charging_status=b.get("charging_status", "unknown"),
        )
    model = ident.get("model_name", "Unknown")
    size = (root or {}).get("size_bytes", 0)
    avail = (root or {}).get("available_bytes", 0)
    return Snapshot(
        snapshot_id=f"{r['device_id']}/{collected.strftime('%Y-%m-%d')}",
        device_id=r["device_id"],
        company_id=r["company_id"],
        employee_id=r.get("employee_id", "unknown"),
        collected_at=collected,
        hostname=os_.get("hostname", ""),
        model_name=model,
        device_class="desktop" if model in DESKTOP_MODELS else "laptop",
        os_name=os_.get("product_name", "Unknown"),
        os_version=os_.get("product_version", ""),
        os_version_tuple=parse_version(os_.get("product_version", "0")),
        ram_gb=total_mem / GIB,
        mem_used_pct=100.0 * used_mem / total_mem if total_mem else 0.0,
        disk_size_gb=size / GIB,
        disk_free_gb=avail / GIB,
        disk_free_pct=100.0 * avail / size if size else 0.0,
        disk_encrypted=bool((root or {}).get("encrypted", False)),
        battery=battery,
        has_network_block="network" in r,
        compliance=tuple(
            Compliance(c["check_id"], c["status"], c.get("severity", "unknown"))
            for c in r.get("compliance_results") or []
        ),
        software=tuple((s.get("name", "?"), s.get("version", "?")) for s in r.get("installed_software") or []),
        raw=r,
    )
