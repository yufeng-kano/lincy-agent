"""Tests for the unified proxy dispatcher."""

from __future__ import annotations

import pytest

import chat_proxy.__main__ as cli


def test_dispatch_forwards_remaining_argv(monkeypatch):
    calls: dict[str, list[str]] = {}

    def fake_main(argv):
        calls["argv"] = list(argv)

    monkeypatch.setitem(cli.PROVIDER_LOADERS, "claude-code", lambda: fake_main)
    cli.main(["claude-code", "tokens", "list"])
    assert calls["argv"] == ["tokens", "list"]


def test_unknown_provider_exits_with_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "unknown provider 'nope'" in err
    assert "claude-code" in err


def test_help_lists_every_provider(capsys):
    cli.main(["--help"])
    out = capsys.readouterr().out
    for provider in cli.PROVIDER_LOADERS:
        assert provider in out


def test_codex_help_lists_login_and_tokens(capsys):
    from codex_proxy.__main__ import main as codex_main

    with pytest.raises(SystemExit):
        codex_main(["--help"])
    out = capsys.readouterr().out
    assert "login" in out
    assert "tokens" in out
    assert "serve" in out
