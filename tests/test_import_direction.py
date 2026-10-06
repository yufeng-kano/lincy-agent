"""Layering rules for src/lincy: host -> agent -> channels, ui never imports agent."""

import re
from pathlib import Path


SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "lincy"

_FORBIDDEN_UI_LIBS = re.compile(
    r"^\s*(?:from|import)\s+(?:textual|prompt_toolkit)\b", re.M
)
_HOST_IMPORT = re.compile(
    r"^\s*(?:from\s+(?:lincy\.host|\.+host)\b|import\s+lincy\.host\b)", re.M
)
_AGENT_IMPORT = re.compile(
    r"^\s*(?:from\s+(?:lincy\.agent|\.\.+agent)\b|import\s+lincy\.agent\b)", re.M
)


def _python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _rel(path: Path) -> str:
    return path.relative_to(SRC_ROOT).as_posix()


def test_no_terminal_ui_libraries():
    offenders = [
        _rel(path)
        for path in _python_files()
        if _FORBIDDEN_UI_LIBS.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_only_entry_point_and_host_import_host():
    offenders = [
        _rel(path)
        for path in _python_files()
        if _HOST_IMPORT.search(path.read_text(encoding="utf-8"))
        and _rel(path) != "__main__.py"
        and not _rel(path).startswith("host/")
    ]
    assert offenders == []


def test_ui_does_not_import_agent():
    offenders = [
        _rel(path)
        for path in sorted((SRC_ROOT / "ui").rglob("*.py"))
        if _AGENT_IMPORT.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_rules_detect_violations():
    # Guard against regexes that silently match nothing.
    assert _FORBIDDEN_UI_LIBS.search("from textual.app import App")
    assert _FORBIDDEN_UI_LIBS.search("import prompt_toolkit")
    assert not _FORBIDDEN_UI_LIBS.search('"(tool-only response; no textual content)"')
    assert _HOST_IMPORT.search("from .host.cli import main")
    assert _HOST_IMPORT.search("    from ..host import init")
    assert _HOST_IMPORT.search("from lincy.host.runtime import HostRuntime")
    assert not _HOST_IMPORT.search('host = "127.0.0.1"')
    assert _AGENT_IMPORT.search("from ..agent.core import AgentCore")
    assert _AGENT_IMPORT.search("from lincy.agent import AgentCore")
    assert not _AGENT_IMPORT.search("from .events import UiEvent")
