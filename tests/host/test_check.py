"""Environment checks: port probe, web UI dist, PATH enrichment, GUI permissions."""

import socket
from types import SimpleNamespace

import pytest

from lincy.host import check
from lincy.host.check import check_environment, enriched_path, port_is_available
from lincy.host.errors import EnvironmentCheckFailed


def _config(port: int):
    return SimpleNamespace(
        app=SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=port)),
        agents={},
    )


@pytest.fixture
def listening_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    yield sock.getsockname()[1]
    sock.close()


@pytest.fixture
def built_repo(tmp_path):
    dist = tmp_path / "src" / "web_ui" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>")
    return tmp_path


def test_port_probe(listening_port):
    assert not port_is_available("127.0.0.1", listening_port)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        free = sock.getsockname()[1]
    assert port_is_available("127.0.0.1", free)


def test_occupied_port_fails_only_when_probing(listening_port, built_repo):
    with pytest.raises(EnvironmentCheckFailed, match="already in use"):
        check_environment(_config(listening_port), built_repo, built_repo, probe_port=True)
    assert check_environment(_config(listening_port), built_repo, built_repo, probe_port=False) is None


def test_missing_dist(tmp_path):
    with pytest.raises(EnvironmentCheckFailed) as exc:
        check_environment(_config(1), tmp_path, tmp_path, probe_port=False)
    assert str(exc.value) == "Web UI is not built. Run: cd src/web_ui && bun run build"


def test_gui_permissions_checked_only_when_gui_enabled(built_repo, monkeypatch):
    seen = []

    def fake_check(state_dir):
        seen.append(state_dir)
        return []

    monkeypatch.setattr(check, "check_gui_permissions", fake_check)
    monkeypatch.setattr(check, "list_input_sources", lambda: ["com.apple.keylayout.ABC"])
    config = _config(1)
    config.agents = {"gui_manager": SimpleNamespace(enabled=False)}
    check_environment(config, built_repo, built_repo / "ws", probe_port=False)
    assert seen == []

    config.agents = {"gui_manager": _gui_config("com.apple.keylayout.ABC")}
    assert check_environment(config, built_repo, built_repo / "ws", probe_port=False) is None
    assert seen == [built_repo / "ws" / "state"]


def test_missing_gui_permissions_stop_startup(built_repo, monkeypatch):
    problems = [
        "Accessibility is not granted (Terminal caveat ...)",
        "Screen Recording is not granted (Terminal caveat ...)",
    ]
    monkeypatch.setattr(check, "check_gui_permissions", lambda state_dir: problems)
    config = _config(1)
    config.agents = {"gui_manager": SimpleNamespace(enabled=True)}
    with pytest.raises(EnvironmentCheckFailed) as exc:
        check_environment(config, built_repo, built_repo, probe_port=False)
    assert str(exc.value) == "\n".join(problems)


def test_enriched_path_prepends_missing_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/opt/homebrew/bin")
    parts = enriched_path().split(":")
    assert parts[-2:] == ["/usr/bin", "/opt/homebrew/bin"]
    assert parts.count("/opt/homebrew/bin") == 1
    assert "/usr/local/bin" in parts
    assert any(p.endswith("/.bun/bin") for p in parts)


def test_missing_binary_fails_before_dist_check(tmp_path, monkeypatch):
    monkeypatch.setattr(
        check.shutil, "which", lambda name, path=None: None if name == "node" else f"/bin/{name}"
    )
    with pytest.raises(EnvironmentCheckFailed, match="Required binaries not found on PATH: node"):
        check_environment(_config(1), tmp_path, tmp_path, probe_port=False)


def test_all_binaries_present_passes(built_repo, monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda name, path=None: f"/bin/{name}")
    assert check_environment(_config(1), built_repo, built_repo, probe_port=False) is None


def _gui_config(default_source: str) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=True, desktop=SimpleNamespace(default_input_source=default_source),
    )


def test_unknown_default_input_source_stops_startup(built_repo, monkeypatch):
    monkeypatch.setattr(check, "check_gui_permissions", lambda state_dir: [])
    monkeypatch.setattr(
        check, "list_input_sources",
        lambda: ["com.apple.keylayout.US", "com.apple.inputmethod.TCIM.Zhuyin"],
    )
    config = _config(1)
    config.agents = {"gui_manager": _gui_config("com.apple.keylayout.ABC")}
    with pytest.raises(EnvironmentCheckFailed, match="com.apple.keylayout.US"):
        check_environment(config, built_repo, built_repo / "ws", probe_port=False)
