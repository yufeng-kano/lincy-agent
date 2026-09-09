"""Session lifetime, nesting, and concurrent task isolation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lincy.llm.session import current_llm_session_key, llm_session


def test_stored_session_survives_resume_and_changes_for_new_session():
    keys = []
    for agent, session in [("brain", "saved"), ("brain", "saved"),
                           ("brain", "new"), ("gui_manager", "saved")]:
        with llm_session(agent, session):
            keys.append(current_llm_session_key())
    assert keys[0] == keys[1]
    assert len(set(keys)) == 3
    assert all(len(key) == 64 and key.isascii() for key in keys)
    assert current_llm_session_key() is None


def test_nested_failure_restores_parent_session():
    with llm_session("brain", "saved"):
        parent = current_llm_session_key()
        with pytest.raises(RuntimeError):
            with llm_session("memory_editor"):
                assert current_llm_session_key() != parent
                raise RuntimeError("failed task")
        assert current_llm_session_key() == parent
    assert current_llm_session_key() is None


def test_decorated_tasks_have_separate_concurrent_sessions():
    barrier = Barrier(2)

    @llm_session("worker")
    def task():
        before = current_llm_session_key()
        barrier.wait(timeout=5)
        assert current_llm_session_key() == before
        return before

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = [future.result() for future in [pool.submit(task), pool.submit(task)]]
    assert first is not None and second is not None and first != second
    assert current_llm_session_key() is None
