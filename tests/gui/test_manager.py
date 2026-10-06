"""Tests for gui/manager.py: governed GUIManager loop over a fake backend."""

import base64
import json
import threading
import time
from unittest.mock import call, patch

import pytest

from lincy.gui.desktop import StaleIndexError, StateReadError
from lincy.gui.manager import _ONE_TOOL_ERROR, _STALE_STUB_SUFFIX, GUIManager, MANAGER_TOOLS
from lincy.gui.session import GUISessionStore
from lincy.llm.schema import ContentPart, LLMResponse, Message, ToolCall

def _jpeg(shade: int) -> ContentPart:
    """A real JPEG, since repeat detection decodes screenshots."""
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (64, 36), (shade, shade, shade)).save(buf, format="JPEG")
    return ContentPart(type="image", media_type="image/jpeg", data=base64.b64encode(buf.getvalue()).decode())


_IMAGE = _jpeg(128)


class FakeSnapshot:
    """Duck-types the parts of ax.Snapshot the manager uses."""

    def __init__(self, text: str, snapshot_id: int, screenshot: ContentPart | None = _IMAGE):
        self._text = text
        self.snapshot_id = snapshot_id
        self.screenshot = screenshot

    def render(self) -> str:
        return self._text


class FakeBackend:
    """Records calls; every action returns a snapshot whose text names the call."""

    default_input_source = "com.apple.keylayout.ABC"

    def __init__(self, *, frozen: bool = False, prepare_error: Exception | None = None):
        self.calls: list[tuple[str, tuple, dict]] = []
        # Screenshots per call, to model UIs that change only visually.
        self.screenshots: list[ContentPart] = []
        self.frozen = frozen
        self.prepare_error = prepare_error
        self.errors: dict[str, Exception] = {}

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name in self.errors:
            raise self.errors[name]
        text = "Window: Same" if self.frozen else f"Window: after {name} #{len(self.calls)}"
        shot = self.screenshots.pop(0) if self.screenshots else _IMAGE
        return FakeSnapshot(text, len(self.calls), shot)

    def prepare(self, app):
        if self.prepare_error is not None:
            raise self.prepare_error
        return self._record("prepare", app)

    def get_state(self):
        return self._record("get_state")

    def open_app(self, name):
        return self._record("open_app", name)

    def click(self, **kwargs):
        return self._record("click", **kwargs)

    def drag(self, x1, y1, x2, y2):
        return self._record("drag", x1, y1, x2, y2)

    def scroll(self, **kwargs):
        return self._record("scroll", **kwargs)

    def type_text(self, text):
        return self._record("type_text", text)

    def press_key(self, key):
        return self._record("press_key", key)

    def set_input_source(self, source_id):
        return self._record("set_input_source", source_id)


class FakeClient:
    """LLM client that replays LLMResponse objects and records requests."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.seen_messages: list[list[Message]] = []
        self.seen_tools: list[list] = []

    def chat(self, messages, response_schema=None, temperature=None):
        raise NotImplementedError

    def chat_with_tools(self, messages, tools, temperature=None):
        self.seen_messages.append(list(messages))
        self.seen_tools.append(list(tools))
        if not self._responses:
            return LLMResponse(content="No more responses.")
        return self._responses.pop(0)


def make_manager(tmp_path, client, backend=None, **kwargs):
    backend = backend or FakeBackend()
    manager = GUIManager(
        client,
        backend,
        "system prompt",
        session_store=GUISessionStore(tmp_path / "gui"),
        **kwargs,
    )
    return manager, backend


def tool(tool_name, id="1", **arguments):
    return LLMResponse(tool_calls=[ToolCall(id=id, name=tool_name, arguments=arguments)])


def done(summary="Task completed.", **extra):
    return tool("done", id="done", summary=summary, **extra)


def tool_messages(client, call_index=-1):
    return [m for m in client.seen_messages[call_index] if m.role == "tool"]


class TestTermination:
    def test_done_is_success(self, tmp_path):
        manager, _ = make_manager(tmp_path, FakeClient([done(report="all good")]))
        result = manager.execute_task("Open Finder")
        assert result.status == "success"
        assert result.summary == "Task completed."
        assert result.report == "all good"
        assert result.steps_used == 0

    def test_fail_is_failed(self, tmp_path):
        manager, _ = make_manager(tmp_path, FakeClient([tool("fail", reason="No app.")]))
        result = manager.execute_task("Open something")
        assert result.status == "failed"
        assert result.summary == "No app."

    def test_report_problem_is_blocked(self, tmp_path):
        manager, _ = make_manager(
            tmp_path, FakeClient([tool("report_problem", problem="TCC dialog.")]),
        )
        result = manager.execute_task("Do a thing")
        assert result.status == "blocked"
        assert result.summary == "TCC dialog."

    def test_text_only_response_is_failed(self, tmp_path):
        manager, _ = make_manager(tmp_path, FakeClient([LLMResponse(content="I give up.")]))
        result = manager.execute_task("Do something")
        assert result.status == "failed"
        assert result.summary == "I give up."

    def test_cancel_before_start_is_failed_without_llm_call(self, tmp_path):
        client = FakeClient([done()])
        manager, backend = make_manager(tmp_path, client, is_cancel_requested=lambda: True)
        result = manager.execute_task("Any task")
        assert result.status == "failed"
        assert "cancel" in result.summary.lower()
        assert client.seen_messages == []
        assert backend.calls == []

    def test_prepare_failure_is_failed_without_llm_call(self, tmp_path):
        client = FakeClient([done()])
        backend = FakeBackend(prepare_error=ValueError("app not found: Nope"))
        manager, _ = make_manager(tmp_path, client, backend)
        result = manager.execute_task("Open Nope", app="Nope")
        assert result.status == "failed"
        assert "app not found: Nope" in result.summary
        assert client.seen_messages == []


class TestOpeningMessage:
    def test_prepare_state_follows_intent(self, tmp_path):
        client = FakeClient([done()])
        manager, backend = make_manager(tmp_path, client)
        manager.execute_task("Fill the form", app="Google Chrome")
        assert backend.calls[0] == ("prepare", ("Google Chrome",), {})
        user = client.seen_messages[0][1]
        assert user.role == "user"
        assert [p.type for p in user.content] == ["text", "text", "image"]
        assert user.content[0].text == "GUI TASK: Fill the form"
        assert user.content[1].text == (
            "Default input source (already selected): com.apple.keylayout.ABC\n\n"
            "Current state:\nState 1\nWindow: after prepare #1"
        )

    def test_tools_are_static_and_wait_can_be_disabled(self, tmp_path):
        client = FakeClient([done()])
        manager, _ = make_manager(tmp_path, client, allow_wait_tool=False)
        manager.execute_task("Anything")
        names = [t.name for t in client.seen_tools[0]]
        assert "wait" not in names
        assert names == [t.name for t in MANAGER_TOOLS if t.name != "wait"]


class TestActions:
    def test_action_result_is_state_text_and_image(self, tmp_path):
        client = FakeClient([tool("click", index=4), done()])
        manager, backend = make_manager(tmp_path, client)
        result = manager.execute_task("Click it")
        assert result.steps_used == 1
        assert backend.calls[1] == (
            "click", (), {"index": 4, "state": None, "x": None, "y": None, "button": "left", "count": 1},
        )
        [msg] = tool_messages(client)
        assert [p.type for p in msg.content] == ["text", "image"]
        assert msg.content[0].text == "State 2\nWindow: after click #2"

    def test_argument_mapping(self, tmp_path):
        client = FakeClient([
            tool("click", id="a", x="10", y=20.0, button="right", count=2),
            tool("scroll", id="b", index=3, direction="up"),
            tool("drag", id="c", x1=1, y1=2, x2=3, y2=4),
            tool("type_text", id="d", text="hello"),
            tool("press_key", id="e", key="Command+A"),
            tool("set_input_source", id="f", source_id="com.apple.keylayout.ABC"),
            tool("open_app", id="g", name="TextEdit"),
            tool("get_state", id="h"),
            done(),
        ])
        manager, backend = make_manager(tmp_path, client)
        manager.execute_task("Exercise tools")
        assert backend.calls[1:] == [
            ("click", (), {"index": None, "state": None, "x": 10, "y": 20, "button": "right", "count": 2}),
            ("scroll", (), {"index": 3, "state": None, "x": None, "y": None, "direction": "up", "amount": 3}),
            ("drag", (1, 2, 3, 4), {}),
            ("type_text", ("hello",), {}),
            ("press_key", ("Command+A",), {}),
            ("set_input_source", ("com.apple.keylayout.ABC",), {}),
            ("open_app", ("TextEdit",), {}),
            ("get_state", (), {}),
        ]

    def test_stale_index_becomes_tool_error(self, tmp_path):
        client = FakeClient([tool("click", index=99), done()])
        backend = FakeBackend()
        backend.errors["click"] = StaleIndexError("index 99")
        manager, _ = make_manager(tmp_path, client, backend)
        result = manager.execute_task("Click stale")
        assert result.status == "success"
        [msg] = tool_messages(client)
        assert msg.content.startswith("Error: index 99. Nothing was clicked.")

    def test_state_read_error_says_action_happened(self, tmp_path):
        client = FakeClient([tool("click", x=5, y=5), done()])
        backend = FakeBackend()
        backend.errors["click"] = StateReadError("AX timeout")
        manager, _ = make_manager(tmp_path, client, backend)
        manager.execute_task("Submit")
        [msg] = tool_messages(client)
        assert "WAS performed" in msg.content
        assert "Do not repeat the action" in msg.content

    def test_llm_error_pauses_with_last_state(self, tmp_path):
        class FailingClient(FakeClient):
            def chat_with_tools(self, messages, tools, temperature=None):
                if len(self.seen_messages) == 1:
                    raise TimeoutError("provider timeout")
                return super().chat_with_tools(messages, tools, temperature)

        client = FailingClient([tool("type_text", text="hi")])
        manager, _ = make_manager(tmp_path, client)
        result = manager.execute_task("Type")
        assert result.status == "paused"
        assert "provider timeout" in result.summary
        data = manager.session_store.load(result.session_id)
        assert data.status == "paused"
        assert data.steps_used == 1
        assert data.last_state_text.startswith("State 2")

    def test_backend_error_and_missing_argument_are_tool_errors(self, tmp_path):
        client = FakeClient([
            tool("press_key", id="a", key="Hyper+Q"),
            tool("type_text", id="b"),
            tool("bogus", id="c"),
            done(),
        ])
        backend = FakeBackend()
        backend.errors["press_key"] = ValueError("unsupported key: Hyper")
        manager, _ = make_manager(tmp_path, client, backend)
        result = manager.execute_task("Errors")
        assert result.status == "success"
        assert result.steps_used == 3
        texts = [m.content for m in tool_messages(client)]
        assert texts == [
            "Error: unsupported key: Hyper",
            "Error: missing required argument 'text'",
            "Error: unknown tool: bogus",
        ]


class TestOneToolPerResponse:
    def test_only_first_call_runs(self, tmp_path):
        client = FakeClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="click", arguments={"index": 1}),
                ToolCall(id="2", name="click", arguments={"index": 2}),
                ToolCall(id="3", name="done", arguments={"summary": "too early"}),
            ]),
            done("real done"),
        ])
        manager, backend = make_manager(tmp_path, client)
        result = manager.execute_task("Click once")
        assert [c[0] for c in backend.calls] == ["prepare", "click"]
        assert result.steps_used == 1
        assert result.summary == "real done"
        msgs = tool_messages(client)
        assert [m.tool_call_id for m in msgs] == ["1", "2", "3"]
        assert msgs[1].content == _ONE_TOOL_ERROR
        assert msgs[2].content == _ONE_TOOL_ERROR

    def test_terminal_first_wins(self, tmp_path):
        client = FakeClient([
            LLMResponse(tool_calls=[
                ToolCall(id="1", name="done", arguments={"summary": "finished"}),
                ToolCall(id="2", name="click", arguments={"index": 1}),
            ]),
        ])
        manager, backend = make_manager(tmp_path, client)
        result = manager.execute_task("Finish")
        assert result.status == "success"
        assert [c[0] for c in backend.calls] == ["prepare"]


class TestPause:
    def test_max_steps_pauses_with_situation_report(self, tmp_path):
        client = FakeClient(
            [tool("click", id=str(i), index=i) for i in range(3)]
            + [LLMResponse(content="Half the form is filled.")]
        )
        manager, _ = make_manager(tmp_path, client, max_steps=3)
        result = manager.execute_task("Fill")
        assert result.status == "paused"
        assert "3 steps" in result.summary
        assert result.report == "Half the form is filled."
        assert client.seen_tools[-1] == []

    def test_repeat_with_unchanged_state_pauses(self, tmp_path):
        client = FakeClient(
            [tool("press_key", id=str(i), key="Up") for i in range(3)]
            + [LLMResponse(content="Up does nothing here.")]
        )
        manager, _ = make_manager(tmp_path, client, FakeBackend(frozen=True), repeat_limit=3)
        result = manager.execute_task("Scroll list")
        assert result.status == "paused"
        assert result.steps_used == 3
        assert "repeated 3 times" in result.summary
        assert result.report == "Up does nothing here."

    def test_repeat_by_index_with_advancing_state_pauses(self, tmp_path):
        # Each call carries the newest state number; the action is still the same.
        client = FakeClient(
            [tool("click", id=str(i), index=4, state=i + 1) for i in range(3)]
            + [LLMResponse(content="Clicking does nothing.")]
        )
        manager, _ = make_manager(tmp_path, client, FakeBackend(frozen=True), repeat_limit=3)
        result = manager.execute_task("Press the button")
        assert result.status == "paused"
        assert result.steps_used == 3
        assert "repeated 3 times" in result.summary

    def test_repeat_with_only_screenshot_changing_continues(self, tmp_path):
        client = FakeClient(
            [tool("scroll", id=str(i), x=10, y=10, direction="down") for i in range(4)] + [done()]
        )
        backend = FakeBackend(frozen=True)
        backend.screenshots = [_IMAGE, _jpeg(0), _jpeg(255), _jpeg(0), _jpeg(255)]
        manager, _ = make_manager(tmp_path, client, backend, repeat_limit=3)
        result = manager.execute_task("Scroll a canvas")
        assert result.status == "success"
        assert result.steps_used == 4

    def test_repeat_with_changing_state_continues(self, tmp_path):
        client = FakeClient(
            [tool("press_key", id=str(i), key="Down") for i in range(4)] + [done()]
        )
        manager, _ = make_manager(tmp_path, client, repeat_limit=3)
        result = manager.execute_task("Move down")
        assert result.status == "success"
        assert result.steps_used == 4


class TestPacingAndCancel:
    def test_wait_returns_fresh_state_and_can_be_cancelled(self, tmp_path):
        cancel = threading.Event()
        client = FakeClient([tool("wait", seconds=5.0), done()])
        manager, _ = make_manager(tmp_path, client, is_cancel_requested=cancel.is_set)
        timer = threading.Timer(0.1, cancel.set)
        start = time.monotonic()
        timer.start()
        try:
            result = manager.execute_task("Wait then cancel")
        finally:
            timer.cancel()
        assert result.status == "failed"
        assert "cancel" in result.summary.lower()
        assert time.monotonic() - start < 2.0

    @patch("time.sleep")
    @patch("lincy.gui.manager.random.uniform", return_value=0.25)
    def test_step_delay_after_each_action(self, mock_uniform, mock_sleep, tmp_path):
        client = FakeClient([
            tool("type_text", id="1", text="hello"),
            tool("press_key", id="2", key="Return"),
            done(),
        ])
        manager, _ = make_manager(tmp_path, client, step_delay_min=0.2, step_delay_max=0.3)
        assert manager.execute_task("Type and submit").status == "success"
        assert mock_uniform.call_args_list == [call(0.2, 0.3), call(0.2, 0.3)]
        assert mock_sleep.call_args_list == [call(0.25), call(0.25)]

    def test_on_step_gets_first_line_and_budget(self, tmp_path):
        seen = []
        client = FakeClient([tool("get_state"), done()])
        manager, _ = make_manager(
            tmp_path, client, max_steps=7, on_step=lambda *args: seen.append(args),
        )
        manager.execute_task("Look")
        assert [(a[0].name, a[1], a[2], a[3]) for a in seen] == [
            ("get_state", "State 2", 1, 7),
            ("done", "Task completed.", 2, 7),
        ]


class TestStaleStateCollapse:
    def _state(self, idx):
        return Message(
            role="tool", tool_call_id=str(idx), name="get_state",
            content=[
                ContentPart(type="text", text=f"State {idx}\nline2"),
                ContentPart(type="image", media_type="image/jpeg", data="QUJD"),
            ],
        )

    def test_only_newest_k_states_keep_payload(self, tmp_path):
        manager, _ = make_manager(tmp_path, FakeClient([]), keep_full_states=2)
        messages = [self._state(i) for i in range(4)]
        manager._collapse_stale_states(messages)
        assert isinstance(messages[0].content, str)
        assert messages[0].content.startswith("State 0\nline2")
        assert _STALE_STUB_SUFFIX in messages[0].content
        assert isinstance(messages[1].content, str)
        assert isinstance(messages[2].content, list)
        assert isinstance(messages[3].content, list)

    def test_stale_text_is_capped(self, tmp_path):
        manager, _ = make_manager(
            tmp_path, FakeClient([]), keep_full_states=1, stale_text_max_chars=200,
        )
        messages = [
            Message(role="tool", tool_call_id="1", name="get_state",
                    content=[ContentPart(type="text", text="x" * 5000)]),
            Message(role="tool", tool_call_id="2", name="get_state",
                    content=[ContentPart(type="text", text="fresh")]),
        ]
        manager._collapse_stale_states(messages)
        assert len(messages[0].content) < 400
        assert "[text truncated]" in messages[0].content

    def test_collapse_runs_between_llm_turns(self, tmp_path):
        client = FakeClient([tool("get_state", id=str(i)) for i in range(4)] + [done()])
        manager, _ = make_manager(tmp_path, client, keep_full_states=1)
        manager.execute_task("Observe repeatedly")
        multimodal = [m for m in tool_messages(client) if isinstance(m.content, list)]
        assert len(multimodal) == 1


class TestSessionAndResume:
    def test_steps_full_text_and_final_state_persisted(self, tmp_path):
        client = FakeClient([tool("type_text", text="hi"), done(report="typed")])
        manager, _ = make_manager(tmp_path, client)
        result = manager.execute_task("Type", app="TextEdit")
        store = manager.session_store
        data = store.load(result.session_id)
        assert data.status == "completed"
        assert data.app == "TextEdit"
        assert data.steps_used == 1
        assert data.last_state_text == "State 2\nWindow: after type_text #2"
        lines = (tmp_path / "gui" / f"{result.session_id}.steps.jsonl").read_text().splitlines()
        [step] = [json.loads(line) for line in lines]
        assert step["tool"] == "type_text"
        assert step["args"] == {"text": "hi"}
        assert step["result"] == "State 2\nWindow: after type_text #2"
        assert result.screenshot_path == str(tmp_path / "gui" / f"{result.session_id}.jpg")
        assert (tmp_path / "gui" / f"{result.session_id}.jpg").read_bytes() == base64.b64decode(_IMAGE.data)

    def test_resume_injects_report_state_and_screenshot(self, tmp_path):
        client = FakeClient([
            tool("click", index=1),
            tool("report_problem", problem="Need the file name.", report="Dialog open."),
            done(),
        ])
        manager, backend = make_manager(tmp_path, client)
        first = manager.execute_task("Upload", app="Google Chrome")
        assert first.status == "blocked"
        assert manager.session_store.load(first.session_id).status == "blocked"

        second = manager.execute_task("Pick report.pdf", session_id=first.session_id)
        assert second.session_id == first.session_id
        assert second.steps_used == 0
        assert backend.calls[-1] == ("prepare", ("Google Chrome",), {})
        user = client.seen_messages[-1][1]
        assert [p.type for p in user.content] == ["text", "image", "text", "image"]
        intro = user.content[0].text
        assert intro.startswith(f"Resuming GUI session {first.session_id}.")
        assert "Previous report:\nDialog open." in intro
        assert "Last observed state:\nState 2\nWindow: after click #2" in intro
        assert intro.endswith("New instruction: Pick report.pdf")
        assert user.content[1].data == _IMAGE.data
        assert manager.session_store.load(first.session_id).status == "completed"

    def test_unknown_session_raises(self, tmp_path):
        manager, _ = make_manager(tmp_path, FakeClient([]))
        with pytest.raises(FileNotFoundError):
            manager.execute_task("Resume", session_id="nope")

    def test_llm_session_key_shared_by_resume(self, tmp_path):
        from lincy.llm.session import current_llm_session_key

        client = FakeClient([done(), done(), done()])
        keys = []
        original = client.chat_with_tools

        def record(messages, tools, temperature=None):
            keys.append(current_llm_session_key())
            return original(messages, tools, temperature)

        client.chat_with_tools = record
        manager, _ = make_manager(tmp_path, client)
        first = manager.execute_task("first")
        manager.execute_task("continue", session_id=first.session_id)
        manager.execute_task("new task")
        assert keys[0] is not None
        assert keys[0] == keys[1]
        assert keys[2] != keys[0]
        assert current_llm_session_key() is None
