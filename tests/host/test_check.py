"""Environment checks: port probe, web UI dist, PATH enrichment."""

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
        check_environment(_config(listening_port), built_repo, probe_port=True)
    assert check_environment(_config(listening_port), built_repo, probe_port=False) is None


def test_missing_dist(tmp_path):
    with pytest.raises(EnvironmentCheckFailed) as exc:
        check_environment(_config(1), tmp_path, probe_port=False)
    assert str(exc.value) == "Web UI is not built. Run: cd src/web_ui && bun run build"


def test_gui_enabled_returns_binary(built_repo, monkeypatch):
    gm = SimpleNamespace(enabled=True, ax=SimpleNamespace(binary_path=None, repo=None, commit=None))
    config = _config(1)
    config.agents = {"gui_manager": gm}
    monkeypatch.setattr(check, "ensure_binary", lambda **kwargs: "/bin/ax")
    assert check_environment(config, built_repo, probe_port=False) == "/bin/ax"

    def failing(**kwargs):
        raise check.AXRuntimeError("swift toolchain not found")

    monkeypatch.setattr(check, "ensure_binary", failing)
    with pytest.raises(EnvironmentCheckFailed, match="swift toolchain not found"):
        check_environment(config, built_repo, probe_port=False)


def test_enriched_path_prepends_missing_dirs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/opt/homebrew/bin")
    parts = enriched_path().split(":")
    assert parts[-2:] == ["/usr/bin", "/opt/homebrew/bin"]
    assert parts.count("/opt/homebrew/bin") == 1
    assert "/usr/local/bin" in parts
    assert any(p.endswith("/.bun/bin") for p in parts)
