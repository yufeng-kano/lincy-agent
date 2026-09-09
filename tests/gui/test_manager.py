"""Tests for gui/manager.py: AX-first GUIManager agentic loop."""

import threading
import time
from unittest.mock import call, patch

from lincy.gui.manager import (
    _STALE_STUB_SUFFIX,
    GUIManager,
    MANAGER_TOOLS,
    MCP_TOOL_DEFS,
)
from lincy.gui.mcp_client import MCPError
from lincy.gui.session import GUISessionStore
from lincy.llm.schema import ContentPart, LLMResponse, Message, ToolCall


class FakeManagerClient:
    """LLM client that returns a sequence of LLMResponse objects."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self._idx = 0
        self.seen_messages: list[list[Message]] = []

    def chat(self, messages, response_schema=None, temperature=None):
        raise NotImplementedError

    def chat_with_tools(self, messages, tools, temperature=None):
        self.seen_messages.append(list(messages))
        if self._idx >= len(self._responses):
            return LLMResponse(content="No more responses.")
        resp = self._responses[self._idx]
        self._idx += 1
        return resp


class FakeMCP:
    """Stands in for MCPStdioClient: records calls, replays canned results."""

    def __init__(self, results=None, fail_init=False):
        # results: dict tool_name -> (text, images, is_error) or Exception
        self.results = results or {}
        self.fail_init = fail_init
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.initialized = False

    def initialize(self):
        if self.fail_init:
            raise MCPError("spawn failed")
        self.initialized = True
        return {}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        outcome = self.results.get(name, ("ok", [], False))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self):
        self.closed = True


class FakeImage:
    def __init__(self):
        self.data = "QUJD"
        self.mime_type = "image/png"


def make_manager(client, mcp=None, **kwargs):
    mcp = mcp or FakeMCP()
    manager = GUIManager(
        client,
        mcp_factory=lambda: mcp,
        system_prompt="system prompt",
        **kwargs,
    )
    return manager, mcp


def done_response(summary="Task completed."):
    return LLMResponse(tool_calls=[
        ToolCall(id="done", name="done", arguments={"summary": summary}),
    ])


class TestGUIManagerTermination:
    def test_done_returns_success_and_closes_mcp(self):
        client = FakeManagerClient([done_response()])
        manager, mcp = make_manager(client)
        result = manager.execute_task("Open Finder")
        assert result.success is True
        assert "completed" in result.summary
        assert mcp.initialized is True
        assert mcp.closed is True

    def test_fail_returns_failure(self):
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="fail", arguments={"reason": "Could not find app."}),
            ]),
        ])
        manager, _ = make_manager(client)
        result = manager.execute_task("Open something")
        assert result.success is False
        assert "Could not find" in result.summary

    def test_report_problem_sets_needs_input(self):
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="report_problem",
                         arguments={"problem": "Wrong screen."}),
            ]),
        ])
        manager, _ = make_manager(client)
        result = manager.execute_task("Do a thing")
        assert result.success is False
        assert result.needs_input is True


class TestGUIManagerMCPDispatch:
    def test_get_app_state_returns_multimodal_and_counts_step(self):
        mcp = FakeMCP(results={
            "get_app_state": ("App=x\n0 window", [FakeImage()], False),
        })
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "Finder"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        result = manager.execute_task("Observe Finder")
        assert result.success is True
        assert result.steps_used == 1
        assert mcp.calls == [("get_app_state", {"app": "Finder"})]
        # Tool result message carries text + image parts
        tool_msgs = [
            m for msgs in client.seen_messages for m in msgs
            if m.role == "tool" and isinstance(m.content, list)
        ]
        assert tool_msgs, "expected a multimodal tool result"
        types = [p.type for p in tool_msgs[0].content]
        assert types == ["text", "image"]

    def test_tree_limits_injected_from_config(self):
        mcp = FakeMCP(results={"get_app_state": ("tree", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "Finder"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(
            client, mcp=mcp, max_tree_nodes=300, max_tree_depth=12,
        )
        manager.execute_task("Observe")
        assert mcp.calls[0][1] == {
            "app": "Finder", "max_tree_nodes": 300, "max_tree_depth": 12,
        }

    def test_model_supplied_tree_limits_win(self):
        mcp = FakeMCP(results={"get_app_state": ("tree", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state",
                         arguments={"app": "Finder", "max_tree_nodes": 50}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp, max_tree_nodes=300)
        manager.execute_task("Observe")
        assert mcp.calls[0][1]["max_tree_nodes"] == 50

    def test_mcp_error_becomes_tool_error_and_loop_continues(self):
        mcp = FakeMCP(results={"click": MCPError("server gone")})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="click",
                         arguments={"app": "Finder", "element_index": "3"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        result = manager.execute_task("Click it")
        assert result.success is True  # loop survived the tool error
        error_results = [
            m for msgs in client.seen_messages for m in msgs
            if m.role == "tool" and isinstance(m.content, str)
            and m.content.startswith("Error:")
        ]
        assert error_results

    def test_is_error_result_prefixed(self):
        mcp = FakeMCP(results={"get_app_state": ("appNotFound", [], True)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "Nope"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        manager.execute_task("Observe")
        tool_texts = [
            m.content for msgs in client.seen_messages for m in msgs
            if m.role == "tool" and isinstance(m.content, str)
        ]
        assert any(t.startswith("Error:") for t in tool_texts)

    def test_init_failure_returns_clean_failure_without_llm_call(self):
        mcp = FakeMCP(fail_init=True)
        client = FakeManagerClient([done_response()])
        manager, _ = make_manager(client, mcp=mcp)
        result = manager.execute_task("Anything")
        assert result.success is False
        assert "unavailable" in result.summary
        assert client.seen_messages == []
        assert mcp.closed is True


class TestGUIManagerLimits:
    def test_max_steps_exceeded(self):
        mcp = FakeMCP(results={"get_app_state": ("tree", [], False)})
        responses = [
            LLMResponse(tool_calls=[
                ToolCall(id=str(i), name="get_app_state", arguments={"app": "F"}),
            ])
            for i in range(25)
        ]
        client = FakeManagerClient(responses)
        manager, _ = make_manager(client, mcp=mcp, max_steps=3)
        result = manager.execute_task("Keep looking")
        assert result.success is False
        assert "Exceeded" in result.summary

    def test_no_tool_calls_returns_failure(self):
        client = FakeManagerClient([LLMResponse(content="I cannot do this task.")])
        manager, _ = make_manager(client)
        result = manager.execute_task("Do something")
        assert result.success is False
        assert result.steps_used == 0

    def test_cancel_before_first_llm_call_returns_cancelled(self):
        client = FakeManagerClient([done_response("should not happen")])
        manager, mcp = make_manager(client, is_cancel_requested=lambda: True)
        result = manager.execute_task("Any task")
        assert result.success is False
        assert "cancel" in result.summary.lower()
        assert result.steps_used == 0
        assert mcp.closed is True

    def test_wait_tool_can_be_cancelled_mid_sleep(self):
        responses = [
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="wait", arguments={"seconds": 5.0}),
            ]),
            done_response(),
        ]
        client = FakeManagerClient(responses)
        cancel_event = threading.Event()
        manager, _ = make_manager(
            client, is_cancel_requested=cancel_event.is_set,
        )
        timer = threading.Timer(0.1, cancel_event.set)
        start = time.monotonic()
        timer.start()
        try:
            result = manager.execute_task("Wait then cancel")
        finally:
            timer.cancel()
        elapsed = time.monotonic() - start
        assert result.success is False
        assert "cancel" in result.summary.lower()
        assert elapsed < 2.0

    @patch("time.sleep")
    @patch("lincy.gui.manager.random.uniform", return_value=0.25)
    def test_step_delay_applies_after_each_non_terminal_tool(
        self, mock_uniform, mock_sleep,
    ):
        mcp = FakeMCP(results={
            "type_text": ("typed", [], False),
            "press_key": ("pressed", [], False),
        })
        responses = [
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="type_text",
                         arguments={"app": "T", "text": "hello"}),
                ToolCall(id="2", name="press_key",
                         arguments={"app": "T", "key": "Return"}),
            ]),
            done_response(),
        ]
        client = FakeManagerClient(responses)
        manager, _ = make_manager(
            client, mcp=mcp, step_delay_min=0.2, step_delay_max=0.3,
        )
        result = manager.execute_task("Type and submit")
        assert result.success is True
        assert mock_uniform.call_args_list == [call(0.2, 0.3), call(0.2, 0.3)]
        assert mock_sleep.call_args_list == [call(0.25), call(0.25)]


class TestStaleStateCollapse:
    def _state_message(self, idx):
        return Message(
            role="tool",
            tool_call_id=str(idx),
            name="get_app_state",
            content=[
                ContentPart(type="text", text=f"App state {idx}\nline2"),
                ContentPart(type="image", media_type="image/png", data="QUJD"),
            ],
        )

    def test_only_newest_k_states_keep_payload(self):
        client = FakeManagerClient([])
        manager, _ = make_manager(client, keep_full_states=2)
        messages = [Message(role="system", content="s")]
        for i in range(4):
            messages.append(Message(role="assistant", content="", tool_calls=[]))
            messages.append(self._state_message(i))
        manager._collapse_stale_states(messages)
        tool_msgs = [m for m in messages if m.role == "tool"]
        assert isinstance(tool_msgs[0].content, str)
        assert tool_msgs[0].content.startswith("App state 0")
        # Observed text survives pruning; only the screenshot is dropped.
        assert "line2" in tool_msgs[0].content
        assert _STALE_STUB_SUFFIX in tool_msgs[0].content
        assert isinstance(tool_msgs[1].content, str)
        assert isinstance(tool_msgs[2].content, list)
        assert isinstance(tool_msgs[3].content, list)

    def test_stale_text_is_capped(self):
        client = FakeManagerClient([])
        manager, _ = make_manager(
            client, keep_full_states=1, stale_text_max_chars=200,
        )
        long_text = "x" * 5000
        messages = [
            Message(
                role="tool", tool_call_id="1", name="get_app_state",
                content=[ContentPart(type="text", text=long_text)],
            ),
            Message(
                role="tool", tool_call_id="2", name="get_app_state",
                content=[ContentPart(type="text", text="fresh")],
            ),
        ]
        manager._collapse_stale_states(messages)
        pruned = messages[0].content
        assert isinstance(pruned, str)
        assert len(pruned) < 400
        assert "[text truncated]" in pruned

    def test_collapse_runs_between_llm_turns(self):
        mcp = FakeMCP(results={
            "get_app_state": ("App tree", [FakeImage()], False),
        })
        responses = [
            LLMResponse(tool_calls=[
                ToolCall(id=str(i), name="get_app_state", arguments={"app": "F"}),
            ])
            for i in range(4)
        ] + [done_response()]
        client = FakeManagerClient(responses)
        manager, _ = make_manager(client, mcp=mcp, keep_full_states=1)
        manager.execute_task("Observe repeatedly")
        # In the final LLM call, only the newest state stays multimodal.
        final_messages = client.seen_messages[-1]
        multimodal = [
            m for m in final_messages
            if m.role == "tool" and isinstance(m.content, list)
        ]
        assert len(multimodal) == 1


class TestArgumentFiltering:
    def test_set_value_empty_string_preserved(self):
        mcp = FakeMCP(results={"set_value": ("ok", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="set_value",
                         arguments={"app": "T", "element_index": "5", "value": ""}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        manager.execute_task("Clear the field")
        assert mcp.calls == [
            ("set_value", {"app": "T", "element_index": "5", "value": ""}),
        ]

    def test_empty_element_index_dropped_for_coordinate_click(self):
        mcp = FakeMCP(results={"click": ("ok", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="click",
                         arguments={"app": "T", "element_index": "",
                                    "x": 10, "y": 20}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        manager.execute_task("Click by coordinates")
        assert mcp.calls == [("click", {"app": "T", "x": 10, "y": 20})]


class TestFinalScreenshot:
    def test_last_mcp_screenshot_saved_to_result(self, tmp_path):
        mcp = FakeMCP(results={
            "get_app_state": ("tree", [FakeImage()], False),
        })
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "F"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        manager._capture_temp = str(tmp_path / "cap.png")
        result = manager.execute_task("Observe")
        assert result.screenshot_path == str(tmp_path / "cap.png")
        assert (tmp_path / "cap.png").read_bytes() == b"ABC"
        assert manager.capture_dir == str(tmp_path)

    def test_no_screenshot_leaves_path_empty(self):
        mcp = FakeMCP(results={"get_app_state": ("tree", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "F"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp)
        result = manager.execute_task("Observe")
        assert result.screenshot_path == ""


class TestGUIManagerSession:
    def test_steps_recorded_and_finalized(self, tmp_path):
        store = GUISessionStore(tmp_path / "gui")
        mcp = FakeMCP(results={"get_app_state": ("tree", [], False)})
        client = FakeManagerClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="get_app_state", arguments={"app": "F"}),
            ]),
            done_response(),
        ])
        manager, _ = make_manager(client, mcp=mcp, session_store=store)
        result = manager.execute_task("Observe")
        assert result.session_id
        data = store.load(result.session_id)
        assert len(data.steps) == 1
        assert data.steps[0].tool == "get_app_state"


class TestManagerToolDefinitions:
    def test_tool_surface(self):
        names = {t.name for t in MANAGER_TOOLS}
        assert names == {
            "list_apps", "get_app_state", "click", "drag", "scroll",
            "type_text", "press_key", "set_value", "perform_secondary_action",
            "wait", "done", "fail", "report_problem",
        }
        assert len(MCP_TOOL_DEFS) == 9

    def test_wait_tool_can_be_disabled(self):
        client = FakeManagerClient([])
        manager, _ = make_manager(client, allow_wait_tool=False)
        assert all(t.name != "wait" for t in manager._tools)

    def test_get_app_state_exposes_text_limit(self):
        get_state = next(t for t in MCP_TOOL_DEFS if t.name == "get_app_state")
        assert "text_limit" in get_state.parameters


def test_gui_session_covers_loop_and_resume(tmp_path):
    from lincy.llm.session import current_llm_session_key

    client = FakeManagerClient([
        LLMResponse(tool_calls=[ToolCall(id="state", name="get_app_state", arguments={"app": "test"})]),
        done_response(), done_response(), done_response(),
    ])
    keys = []
    original = client.chat_with_tools

    def record(messages, tools, temperature=None):
        keys.append(current_llm_session_key())
        return original(messages, tools, temperature)

    client.chat_with_tools = record
    manager, _ = make_manager(client, session_store=GUISessionStore(tmp_path), step_delay_min=0, step_delay_max=0)
    with patch("lincy.gui.manager.activate_app"), patch("lincy.gui.manager.time.sleep"):
        first = manager.execute_task("first")
        assert first.success
        assert manager.execute_task("continue", session_id=first.session_id).success
        assert manager.execute_task("new task").success
    assert keys[0] is not None
    assert keys[0] == keys[1] == keys[2]
    assert keys[3] != keys[0]
    assert current_llm_session_key() is None
