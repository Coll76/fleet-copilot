"""Append-only, hash-chained audit log.

Every entry stores the SHA-256 of the previous entry. Editing or deleting any line
breaks the chain, which `verify_chain` detects. This is tamper-evident, not
tamper-proof: a production system would also ship entries to write-once storage.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path | str, clock=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._clock = clock or (lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        with open(self.path, "rb") as fh:
            last = fh.read().splitlines()[-1]
        return json.loads(last)["hash"]

    def write(self, event: str, *, tenant_id: str, actor: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            entry = {"ts": self._clock(), "event": event, "tenant_id": tenant_id, "actor": actor,
                     "data": data, "prev_hash": self._last_hash()}
            body = json.dumps(entry, sort_keys=True, default=str)
            entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
            with open(self.path, "a") as fh:
                fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            return entry

    def entries(self, tenant_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]
        return [r for r in rows if tenant_id is None or r["tenant_id"] == tenant_id]

    def verify_chain(self) -> tuple[bool, int | None]:
        """Returns (ok, index_of_first_bad_entry)."""
        prev = GENESIS
        for i, r in enumerate(self.entries()):
            h = r.pop("hash")
            if r["prev_hash"] != prev:
                return False, i
            if hashlib.sha256(json.dumps(r, sort_keys=True, default=str).encode()).hexdigest() != h:
                return False, i
            prev = h
        return True, None
