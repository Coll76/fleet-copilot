import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fleet_copilot.analytics.policy import Policy  # noqa: E402
from fleet_copilot.data.store import TelemetryStore  # noqa: E402
from fleet_copilot.tools.context import Session, ToolContext  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def store():
    return TelemetryStore.from_jsonl()


@pytest.fixture(scope="session")
def policy():
    return Policy.load()


@pytest.fixture(scope="session")
def gt():
    return json.loads((ROOT / "evals" / "ground_truth.json").read_text())


@pytest.fixture
def ctx(store, policy):
    def make(tenant="acme-001", role="it_admin"):
        return ToolContext(Session(tenant, f"admin@{tenant}", role), store.for_tenant(tenant), policy)
    return make
