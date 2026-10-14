"""Tests for the Automation (Apple Events) permission check."""

from pathlib import Path

import pytest

from lincy.tools.builtin.macos_apps import apple_events
from lincy.tools.builtin.macos_apps import eventkit, mail_store, notes_store


@pytest.mark.parametrize("code", [0, -600])
def test_granted_or_not_running_is_not_a_problem(monkeypatch, code):
    monkeypatch.setattr(apple_events, "_permission_status", lambda bundle_id: code)

    assert apple_events.automation_problem("com.apple.Notes", "Notes") is None


@pytest.mark.parametrize(
    ("code", "meaning"),
    [(-1743, "denied"), (-1744, "never asked"), (-50, "unexpected result")],
)
def test_missing_grant_names_app_and_settings_pane(monkeypatch, code, meaning):
    asked: list[str] = []
    monkeypatch.setattr(apple_events, "_permission_status", lambda bundle_id: asked.append(bundle_id) or code)

    problem = apple_events.automation_problem("com.apple.mail", "Mail")

    assert asked == ["com.apple.mail"]
    assert f"Automation access to Mail is not granted ({meaning}, code {code})" in problem
    assert "Privacy & Security > Automation" in problem
    assert "permissions-warmup" in problem


def test_permission_problems_include_automation_per_app(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apple_events, "_permission_status", lambda bundle_id: -1743)

    assert any("Automation access to Reminders" in p for p in eventkit.eventkit_permission_problems())
    assert any("Automation access to Notes" in p for p in notes_store.notes_permission_problems())
    assert any("Automation access to Mail" in p for p in mail_store.mail_permission_problems(tmp_path))
