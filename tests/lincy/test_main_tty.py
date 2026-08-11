import pytest

import lincy.__main__ as main_mod


def test_require_tty_for_chat_cli_passes_when_tty(monkeypatch):
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(main_mod.sys.stdout, "isatty", lambda: True)
    main_mod._require_tty_for_chat_cli()


def test_require_tty_for_chat_cli_fails_when_not_tty(monkeypatch, capsys):
    monkeypatch.setattr(main_mod.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(main_mod.sys.stdout, "isatty", lambda: True)

    with pytest.raises(SystemExit) as exc:
        main_mod._require_tty_for_chat_cli()

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "requires a TTY" in err


def test_resolve_user_reads_dotenv_from_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CHAT_AGENT_USER=dotenv-user\n", encoding="utf-8")
    monkeypatch.delenv("CHAT_AGENT_USER", raising=False)

    assert main_mod._resolve_user(None) == "dotenv-user"


def test_resolve_user_prefers_flag_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CHAT_AGENT_USER=dotenv-user\n", encoding="utf-8")

    assert main_mod._resolve_user("cli-user") == "cli-user"
