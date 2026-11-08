"""Tests for m0187: redeploy the recall prompt."""

from importlib import resources
from pathlib import Path

from lincy.workspace.migrations.m0187_recall_prefetch_speed import M0187RecallPrefetchSpeed

TEMPLATES = Path(str(resources.files("lincy.workspace"))) / "templates" / "kernel"
PROMPT = Path("agents") / "recall" / "prompts" / "system.md"


def test_copies_the_recall_prompt_creating_missing_dirs(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()

    M0187RecallPrefetchSpeed().upgrade(kernel_dir, TEMPLATES)

    deployed = (kernel_dir / PROMPT).read_text(encoding="utf-8")
    assert deployed == (TEMPLATES / PROMPT).read_text(encoding="utf-8")
    assert "Braces select" in deployed
    assert "Do not open with" in deployed
    assert M0187RecallPrefetchSpeed.version == "0.86.1"


def test_overwrites_an_older_prompt(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    (kernel_dir / PROMPT).parent.mkdir(parents=True)
    (kernel_dir / PROMPT).write_text("old prompt", encoding="utf-8")

    M0187RecallPrefetchSpeed().upgrade(kernel_dir, TEMPLATES)

    assert (kernel_dir / PROMPT).read_text(encoding="utf-8") != "old prompt"
