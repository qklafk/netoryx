"""Live tests start containers and wait for panels to come up: give them more time than offline tests."""

from __future__ import annotations

import pytest

LIVE_TIMEOUT_SEC = 900


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts and item.get_closest_marker("timeout") is None:
            item.add_marker(pytest.mark.timeout(LIVE_TIMEOUT_SEC))
