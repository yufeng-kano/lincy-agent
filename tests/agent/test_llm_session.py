"""Brain request scopes follow the persistent session manager."""

from unittest.mock import MagicMock

import pytest

from lincy.agent.core import AgentCore
from lincy.llm.session import current_llm_session_key
from lincy.session import SessionManager


def test_brain_reads_current_session_on_each_turn_and_resume(tmp_path, monkeypatch):
    manager = SessionManager(tmp_path)
    saved = manager.create("test-user", "Test")
    core = MagicMock(spec=AgentCore)
    core.session_mgr = manager
    core._llm_session_id = "instance-fallback"
    core.registry = MagicMock()
    core.config = MagicMock()
    core.client = MagicMock()
    core.conversation = MagicMock()
    core.builder = MagicMock()
    core.console = MagicMock()
    core.memory_edit_allow_failure = False
    core.turn_context = None
    core._get_turn_cancel_callbacks.return_value = (None, None)
    keys = []

    class StopAfterModelCall(Exception):
        pass

    def responder(**kwargs):
        keys.append(current_llm_session_key())
        raise StopAfterModelCall

    monkeypatch.setattr("lincy.agent.core._run_responder", responder)

    def turn():
        with pytest.raises(StopAfterModelCall):
            AgentCore._execute_turn_attempt(
                core, prepared=MagicMock(), output=lambda _: None,
                channel="cli", sender="test", enable_memory_sync=False,
                flush_pending_outbound=False,
            )
        assert current_llm_session_key() is None

    turn()
    turn()
    core.session_mgr = SessionManager(tmp_path)
    core.session_mgr.load(saved)
    turn()
    core.session_mgr.create("test-user", "Test")
    turn()
    assert keys[0] is not None
    assert keys[0] == keys[1] == keys[2]
    assert keys[3] != keys[0]
