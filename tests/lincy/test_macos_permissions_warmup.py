"""Tests for the macOS permissions warmup command."""

from subprocess import CompletedProcess, TimeoutExpired

from lincy import macos_permissions_warmup as warmup


def test_list_prints_known_permission_probes(capsys):
    code = warmup.main(["--list"])

    assert code == 0
    output = capsys.readouterr().out
    assert "calendar: Calendar" in output
    assert "reminders: Reminders" in output
    assert "notes: Notes" in output
    assert "photos: Photos" in output
    assert "mail: Mail" in output


def test_non_macos_exits_before_running(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(warmup.sys, "platform", "linux")
    monkeypatch.setattr(warmup, "_run_task", lambda *args, **kwargs: calls.append(args))

    code = warmup.main([])

    assert code == 2
    assert calls == []
    assert "only supported on macOS" in capsys.readouterr().err


def test_run_task_reports_success(monkeypatch):
    task = warmup.WarmupTask(
        name="mail",
        app_name="Mail",
        script_body="return { ok: true };",
    )

    def fake_run(*args, **kwargs):
        return CompletedProcess(args=args[0], returncode=0, stdout='{"ok":true}\n', stderr="")

    monkeypatch.setattr(warmup.subprocess, "run", fake_run)

    result = warmup._run_task(task, timeout=1)

    assert result.ok is True
    assert result.name == "mail"
    assert "ok=True" in result.detail


def test_run_task_reports_timeout(monkeypatch):
    task = warmup.WarmupTask(
        name="photos",
        app_name="Photos",
        script_body="return { ok: true };",
    )

    def fake_run(*args, **kwargs):
        raise TimeoutExpired(cmd=args[0], timeout=1)

    monkeypatch.setattr(warmup.subprocess, "run", fake_run)

    result = warmup._run_task(task, timeout=1)

    assert result.ok is False
    assert result.detail == "timed out after 1.0s"
