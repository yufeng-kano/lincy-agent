"""Web UI build steps and the source fingerprint stamp."""

import subprocess

import pytest

from lincy.host.web_ui import WebUIBuildFailed, build_web_ui, web_ui_is_current


class FakeBun:
    """Records commands; build writes dist/ like vite, optionally fails a step."""

    def __init__(self, failing: str | None = None):
        self.calls: list[tuple[str, object]] = []
        self.failing = failing

    def __call__(self, cmd, cwd, env):
        line = " ".join(cmd)
        self.calls.append((line, cwd))
        if line == self.failing:
            return subprocess.CompletedProcess(cmd, 1, "", "x" * 3000 + "boom")
        if line == "bun run build":
            (cwd / "dist").mkdir(exist_ok=True)
            (cwd / "dist" / "index.html").write_text("<html></html>")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def web_ui(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.vue").write_text("<template />")
    (tmp_path / "package.json").write_text("{}")
    return tmp_path


def test_build_installs_then_builds_and_stamps(web_ui):
    bun = FakeBun()
    assert not web_ui_is_current(web_ui)

    build_web_ui(web_ui, {}, bun)

    assert bun.calls == [
        ("bun install --frozen-lockfile", web_ui),
        ("bun run build", web_ui),
    ]
    assert web_ui_is_current(web_ui)


@pytest.mark.parametrize("path", ["src/App.vue", "package.json", "src/New.vue"])
def test_any_source_change_makes_dist_stale(web_ui, path):
    build_web_ui(web_ui, {}, FakeBun())
    (web_ui / path).write_text("changed")
    assert not web_ui_is_current(web_ui)


def test_dependencies_and_output_do_not_affect_fingerprint(web_ui):
    build_web_ui(web_ui, {}, FakeBun())
    (web_ui / "node_modules" / ".tmp").mkdir(parents=True)
    (web_ui / "node_modules" / ".tmp" / "tsconfig.app.tsbuildinfo").write_text("x")
    (web_ui / "dist" / "extra.js").write_text("x")
    (web_ui / ".DS_Store").write_text("x")
    assert web_ui_is_current(web_ui)


def test_missing_index_is_not_current_even_with_stamp(web_ui):
    build_web_ui(web_ui, {}, FakeBun())
    (web_ui / "dist" / "index.html").unlink()
    assert not web_ui_is_current(web_ui)


def test_failed_step_reports_tail_and_leaves_no_stamp(web_ui):
    bun = FakeBun(failing="bun install --frozen-lockfile")

    with pytest.raises(WebUIBuildFailed) as exc:
        build_web_ui(web_ui, {}, bun)

    assert str(exc.value).startswith("bun install failed (exit 1): ")
    assert str(exc.value).endswith("boom")
    assert len(str(exc.value)) < 2100
    assert [line for line, _ in bun.calls] == ["bun install --frozen-lockfile"]
    assert not web_ui_is_current(web_ui)
