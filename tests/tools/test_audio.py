"""Tests for tools/builtin/audio.py: AudioAgent and read_audio_by_subagent."""

import base64
from pathlib import Path

import pytest

from lincy.llm.schema import ContentPart
from lincy.tools.builtin.audio import (
    MAX_AUDIO_FILE_BYTES,
    READ_AUDIO_BY_SUBAGENT_DEFINITION,
    AudioAgent,
    _read_audio_data,
    create_read_audio_by_subagent,
)


class FakeAgent:
    def __init__(self, response="transcript", fail=False):
        self.response = response
        self.fail = fail
        self.received: list[list[ContentPart]] = []

    def describe(self, parts):
        self.received.append(parts)
        if self.fail:
            raise RuntimeError("boom")
        return self.response


@pytest.fixture()
def tmp_audio(tmp_path: Path) -> Path:
    path = tmp_path / "voice-message.ogg"
    path.write_bytes(b"OggS" + b"\x00" * 64)
    return path


@pytest.fixture()
def allowed_paths(tmp_path: Path) -> list[str]:
    return [str(tmp_path)]


class TestReadAudioData:
    def test_reads_valid_audio(self, tmp_audio, allowed_paths, tmp_path):
        b64, media_type, size = _read_audio_data(str(tmp_audio), allowed_paths, tmp_path)
        assert media_type == "audio/ogg"
        assert size == tmp_audio.stat().st_size
        assert base64.b64decode(b64) == tmp_audio.read_bytes()

    @pytest.mark.parametrize(
        ("suffix", "media_type"),
        [(".opus", "audio/ogg"), (".mp3", "audio/mpeg"), (".m4a", "audio/mp4"), (".wav", "audio/wav")],
    )
    def test_media_type_by_extension(self, tmp_path, allowed_paths, suffix, media_type):
        path = tmp_path / f"clip{suffix}"
        path.write_bytes(b"x")
        assert _read_audio_data(str(path), allowed_paths, tmp_path)[1] == media_type

    def test_file_not_found(self, allowed_paths, tmp_path):
        with pytest.raises(FileNotFoundError, match="Audio not found"):
            _read_audio_data(str(tmp_path / "missing.ogg"), allowed_paths, tmp_path)

    def test_path_not_allowed(self, tmp_audio, tmp_path):
        with pytest.raises(ValueError, match="Path not allowed"):
            _read_audio_data(str(tmp_audio), ["/some/other/path"], tmp_path)

    def test_unsupported_format(self, tmp_path, allowed_paths):
        path = tmp_path / "clip.webm"
        path.write_bytes(b"x")
        with pytest.raises(ValueError, match="Unsupported audio format"):
            _read_audio_data(str(path), allowed_paths, tmp_path)

    def test_too_large(self, tmp_path, allowed_paths, monkeypatch):
        monkeypatch.setattr("lincy.tools.builtin.audio.MAX_AUDIO_FILE_BYTES", 8)
        path = tmp_path / "big.ogg"
        path.write_bytes(b"x" * 9)
        with pytest.raises(ValueError, match="too large"):
            _read_audio_data(str(path), allowed_paths, tmp_path)


class TestReadAudioBySubagent:
    def test_sends_context_and_audio_part(self, tmp_audio, allowed_paths, tmp_path):
        agent = FakeAgent("Speaker 1: hi. Tone: cheerful.")
        tool = create_read_audio_by_subagent(allowed_paths, tmp_path, agent)

        result = tool(path=str(tmp_audio), context="Alice replying about dinner")

        assert result.startswith(f"[Audio: {tmp_audio} (audio/ogg, ")
        assert result.endswith("Speaker 1: hi. Tone: cheerful.")
        parts = agent.received[0]
        assert parts[0].type == "text" and parts[0].text == "Alice replying about dinner"
        assert parts[1].type == "audio"
        assert parts[1].media_type == "audio/ogg"
        assert base64.b64decode(parts[1].data) == tmp_audio.read_bytes()

    def test_context_required(self, tmp_audio, allowed_paths, tmp_path):
        tool = create_read_audio_by_subagent(allowed_paths, tmp_path, FakeAgent())
        assert tool(path=str(tmp_audio), context="") == "Error: context is required."

    def test_path_required(self, allowed_paths, tmp_path):
        tool = create_read_audio_by_subagent(allowed_paths, tmp_path, FakeAgent())
        assert tool(path="", context="x") == "Error: path is required."

    def test_read_error_is_reported(self, allowed_paths, tmp_path):
        tool = create_read_audio_by_subagent(allowed_paths, tmp_path, FakeAgent())
        result = tool(path=str(tmp_audio_missing := tmp_path / "nope.ogg"), context="x")
        assert result.startswith("Error: Audio not found")
        assert str(tmp_audio_missing) in result

    def test_sub_agent_failure_fallback(self, tmp_audio, allowed_paths, tmp_path):
        tool = create_read_audio_by_subagent(allowed_paths, tmp_path, FakeAgent(fail=True))
        result = tool(path=str(tmp_audio), context="x")
        assert "[Audio:" in result
        assert "(Audio analysis unavailable: boom)" in result


class TestAudioAgent:
    def test_uses_audio_session_and_cache(self, tmp_path):
        class Client:
            calls = 0

            def chat(self, messages, response_schema=None, temperature=None):
                from lincy.llm.session import current_llm_session_key

                assert current_llm_session_key() is not None
                assert messages[0].role == "system"
                assert messages[1].content[1].type == "audio"
                Client.calls += 1
                return "ok"

            def chat_with_tools(self, messages, tools, temperature=None):
                raise NotImplementedError

        agent = AudioAgent(Client(), "listen", cache_dir=tmp_path, model_fingerprint="m")
        parts = [
            ContentPart(type="text", text="ctx"),
            ContentPart(type="audio", media_type="audio/ogg", data="abc"),
        ]
        assert agent.describe(parts) == "ok"
        assert agent.describe(parts) == "ok"
        assert Client.calls == 1


def test_definition_structure():
    assert READ_AUDIO_BY_SUBAGENT_DEFINITION.name == "read_audio_by_subagent"
    assert READ_AUDIO_BY_SUBAGENT_DEFINITION.required == ["path", "context"]
    assert MAX_AUDIO_FILE_BYTES < 20 * 1024 * 1024


def test_empty_reply_is_not_cached(tmp_path):
    class Client:
        replies = ["", "ok"]

        def chat(self, messages, response_schema=None, temperature=None):
            return Client.replies.pop(0)

        def chat_with_tools(self, messages, tools, temperature=None):
            raise NotImplementedError

    agent = AudioAgent(Client(), "listen", cache_dir=tmp_path, model_fingerprint="m")
    parts = [ContentPart(type="audio", media_type="audio/ogg", data="abc")]
    assert agent.describe(parts) == ""
    assert agent.describe(parts) == "ok"
