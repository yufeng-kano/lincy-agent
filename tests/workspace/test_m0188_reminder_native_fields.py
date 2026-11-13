"""Tests for m0188: redeploy the brain prompt with the new reminders_tool fields."""

from importlib import resources
from pathlib import Path

from lincy.workspace.migrations.m0188_reminder_native_fields import M0188ReminderNativeFields

TEMPLATES = Path(str(resources.files("lincy.workspace"))) / "templates" / "kernel"
PROMPT = Path("agents") / "brain" / "prompts" / "system.md"


def test_copies_the_brain_prompt_creating_missing_dirs(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()

    M0188ReminderNativeFields().upgrade(kernel_dir, TEMPLATES)

    deployed = (kernel_dir / PROMPT).read_text(encoding="utf-8")
    assert deployed == (TEMPLATES / PROMPT).read_text(encoding="utf-8")
    assert "early_reminder" in deployed
    assert "urgent=true" in deployed
    assert M0188ReminderNativeFields.version == "0.87.0"


def test_overwrites_an_older_prompt(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    (kernel_dir / PROMPT).parent.mkdir(parents=True)
    (kernel_dir / PROMPT).write_text("old prompt", encoding="utf-8")

    M0188ReminderNativeFields().upgrade(kernel_dir, TEMPLATES)

    assert (kernel_dir / PROMPT).read_text(encoding="utf-8") != "old prompt"
