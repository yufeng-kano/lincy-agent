"""Global test fixtures."""

import pytest

from lincy.timezone_utils import configure as configure_tz
from lincy.tools.builtin.macos_apps import apple_events

# Configure app timezone once for all tests
configure_tz("UTC+8")


@pytest.fixture(autouse=True)
def _granted_automation(monkeypatch):
    # The real TCC answer depends on the machine and on which apps are running.
    monkeypatch.setattr(apple_events, "_permission_status", lambda bundle_id: 0)
