import json
from pathlib import Path
from typing import Any

import pytest

from gh_pool.relay.app import create_app as create_relay
from gh_pool.server.app import create_app as create_server

SNAPSHOT = Path(__file__).parent / "routes.json"


def surface(spec: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            found[f"{method.upper()} {path}"] = sorted(operation.get("responses", {}))
    return dict(sorted(found.items()))


def taken() -> dict[str, dict[str, list[str]]]:
    return {
        "server": surface(create_server().openapi()),
        "relay": surface(create_relay().openapi()),
    }


@pytest.fixture
def recorded():
    return json.loads(SNAPSHOT.read_text(encoding="utf-8"))


def test_no_route_appeared_or_vanished(recorded):
    assert sorted(taken()["server"]) == sorted(recorded["server"])
    assert sorted(taken()["relay"]) == sorted(recorded["relay"])


def test_no_route_changed_the_statuses_it_answers_with(recorded):
    assert taken() == recorded
