from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULT_POLICY = Path(__file__).resolve().parents[3] / "config" / "policy.yaml"


@dataclass(frozen=True)
class Policy:
    raw: dict[str, Any]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_POLICY) -> "Policy":
        with open(path) as fh:
            return cls(yaml.safe_load(fh))

    @property
    def t(self) -> dict[str, Any]:
        return self.raw["thresholds"]

    @property
    def risky_software(self) -> dict[str, dict[str, str]]:
        return self.raw.get("risky_software", {})

    @property
    def actions(self) -> dict[str, Any]:
        return self.raw["actions"]
