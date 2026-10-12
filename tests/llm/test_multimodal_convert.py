"""Tests for multimodal _convert_messages in all three providers."""

from lincy.llm.schema import ContentPart, Message, ToolCall


class TestOpenAICompatMultimodal:
    def _make_client(self):
        from lincy.llm.providers.openai_compat import OpenAICompatibleClient
        return OpenAICompatibleClient(
            model="test",
            base_url="http://localhost",
            request_timeout=10.0,
        )

    def test_tool_result_with_image(self):
        """Multimodal tool result: text stays in tool msg, image deferred to user msg."""
        client = self._make_client()
        messages = [
            Message(
                role="tool",
                content=[
                    ContentPart(type="text", text="Image loaded"),
                    ContentPart(type="image", media_type="image/png", data="abc123", width=100, height=100),
                ],
                tool_call_id="call_1",
                name="read_image",
            ),
        ]
        result = client._convert_messages(messages)
        # tool msg (text only) + user msg (image)
        assert len(result) == 2
        tool_payload = result[0]
        assert tool_payload.role == "tool"
        assert tool_payload.content == "Image loaded"
        user_payload = result[1]
        assert user_payload.role == "user"
        assert isinstance(user_payload.content, list)
        assert user_payload.content[0]["type"] == "image_url"
        assert "data:image/png;base64,abc123" in user_payload.content[0]["image_url"]["url"]

    def test_user_message_with_image(self):
        client = self._make_client()
        messages = [
            Message(
                role="user",
                content=[
                    ContentPart(type="text", text="What is this?"),
                    ContentPart(type="image", media_type="image/jpeg", data="xyz", width=50, height=50),
                ],
            ),
        ]
        result = client._convert_messages(messages)
        assert len(result) == 1
        assert isinstance(result[0].content, list)
        assert result[0].content[0]["type"] == "text"
        assert result[0].content[1]["type"] == "image_url"

    def test_string_content_unchanged(self):
        client = self._make_client()
        messages = [
            Message(role="user", content="hello"),
            Message(role="tool", content="result", tool_call_id="t1", name="foo"),
        ]
        result = client._convert_messages(messages)
        assert result[0].content == "hello"
        assert result[1].content == "result"

    def test_repairs_missing_tool_result_before_next_user(self):
        client = self._make_client()
        messages = [
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="t1", name="gui_task", arguments={"intent": "x"})],
            ),
            Message(role="user", content="next turn"),
        ]
        result = client._convert_messages(messages)
        assert len(result) == 3
        assert result[0].role == "assistant"
        assert result[1].role == "tool"
        assert result[1].tool_call_id == "t1"
        assert result[2].role == "user"

    def test_repairs_missing_tool_result_at_end(self):
        client = self._make_client()
        messages = [
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="t1", name="gui_task", arguments={"intent": "x"})],
            ),
        ]
        result = client._convert_messages(messages)
        assert len(result) == 2
        assert result[0].role == "assistant"
        assert result[1].role == "tool"
        assert result[1].tool_call_id == "t1"

    def test_drops_duplicate_tool_result_in_assistant_tool_group(self):
        client = self._make_client()
        messages = [
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(id="a", name="first", arguments={}),
                    ToolCall(id="b", name="second", arguments={}),
                ],
            ),
            Message(role="tool", content="first result", tool_call_id="a", name="first"),
            Message(role="tool", content="second result", tool_call_id="b", name="second"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="c", name="third", arguments={})],
            ),
            Message(role="tool", content="third result", tool_call_id="c", name="third"),
            Message(role="tool", content="duplicate old result", tool_call_id="b", name="second"),
            Message(role="user", content="next"),
        ]

        result = client._convert_messages(messages)

        assert [m.role for m in result] == [
            "assistant",
            "tool",
            "tool",
            "assistant",
            "tool",
            "user",
        ]
        assert [m.tool_call_id for m in result if m.role == "tool"] == ["a", "b", "c"]


class TestAnthropicMultimodal:
    def _make_client(self):
        from lincy.llm.providers.anthropic import AnthropicClient
        from lincy.core.schema import AnthropicConfig
        config = AnthropicConfig(
            model="test",
            api_key="test-key",
            max_tokens=1024,
        )
        return AnthropicClient(config)

    def test_tool_result_with_image(self):
        client = self._make_client()
        messages = [
            Message(
                role="tool",
                content=[
                    ContentPart(type="text", text="Image loaded"),
                    ContentPart(type="image", media_type="image/png", data="abc123", width=100, height=100),
                ],
                tool_call_id="call_1",
                name="read_image",
            ),
        ]
        system, result = client._convert_messages(messages)
        assert len(result) == 1
        payload = result[0]
        assert payload.role == "user"
        assert isinstance(payload.content, list)
        # Should be a tool_result content block with nested image
        block = payload.content[0]
        assert isinstance(block, dict)
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "call_1"
        inner = block["content"]
        assert len(inner) == 2
        assert inner[0]["type"] == "text"
        assert inner[1]["type"] == "image"
        assert inner[1]["source"]["data"] == "abc123"

    def test_user_message_with_image(self):
        client = self._make_client()
        messages = [
            Message(
                role="user",
                content=[
                    ContentPart(type="text", text="What is this?"),
                    ContentPart(type="image", media_type="image/jpeg", data="xyz", width=50, height=50),
                ],
            ),
        ]
        system, result = client._convert_messages(messages)
        assert len(result) == 1
        payload = result[0]
        assert payload.role == "user"
        # Content should be list of dicts (Anthropic blocks)
        assert isinstance(payload.content, list)


class TestGeminiMultimodal:
    def _make_client(self):
        from lincy.llm.providers.gemini import GeminiClient
        from lincy.core.schema import GeminiConfig
        config = GeminiConfig(
            model="test",
            api_key="test-key",
        )
        return GeminiClient(config)

    def test_tool_result_with_image(self):
        client = self._make_client()
        messages = [
            Message(
                role="tool",
                content=[
                    ContentPart(type="text", text="Image loaded"),
                    ContentPart(type="image", media_type="image/png", data="abc123", width=100, height=100),
                ],
                tool_call_id="call_1",
                name="read_image",
            ),
        ]
        system, contents = client._convert_messages(messages)
        assert len(contents) == 1
        content = contents[0]
        assert content.role == "user"
        # Should have function_response + inline_data parts
        assert len(content.parts) == 2
        assert content.parts[0].function_response is not None
        assert content.parts[0].function_response.response["result"] == "Image loaded"
        assert content.parts[1].inline_data is not None
        assert content.parts[1].inline_data.data == "abc123"
        assert content.parts[1].inline_data.mime_type == "image/png"

    def test_user_message_with_image(self):
        client = self._make_client()
        messages = [
            Message(
                role="user",
                content=[
                    ContentPart(type="text", text="What is this?"),
                    ContentPart(type="image", media_type="image/jpeg", data="xyz", width=50, height=50),
                ],
            ),
        ]
        system, contents = client._convert_messages(messages)
        assert len(contents) == 1
        content = contents[0]
        assert content.role == "user"
        assert len(content.parts) == 2
        assert content.parts[0].text == "What is this?"
        assert content.parts[1].inline_data is not None
        assert content.parts[1].inline_data.mime_type == "image/jpeg"


class TestAudioParts:
    def _audio_message(self, role="user", **kwargs):
        return Message(
            role=role,
            content=[
                ContentPart(type="text", text="listen"),
                ContentPart(type="audio", media_type="audio/ogg", data="b64"),
            ],
            **kwargs,
        )

    def test_openai_compat_maps_input_audio(self):
        client = TestOpenAICompatMultimodal._make_client(self)
        result = client._convert_messages([self._audio_message()])
        assert result[0].content[1] == {
            "type": "input_audio",
            "input_audio": {"data": "b64", "format": "ogg"},
        }

    def test_openai_compat_defers_tool_result_audio_to_user_message(self):
        client = TestOpenAICompatMultimodal._make_client(self)
        result = client._convert_messages(
            [self._audio_message(role="tool", tool_call_id="c1", name="read_audio")]
        )
        assert result[0].role == "tool"
        assert result[0].content == "listen"
        assert result[1].role == "user"
        assert result[1].content[0]["type"] == "input_audio"

    def test_openai_compat_rejects_unknown_audio_media_type(self):
        import pytest

        client = TestOpenAICompatMultimodal._make_client(self)
        message = Message(
            role="user",
            content=[ContentPart(type="audio", media_type="audio/webm", data="b64")],
        )
        with pytest.raises(ValueError, match="Unsupported audio media type"):
            client._convert_messages([message])

    def test_gemini_maps_inline_data(self):
        client = TestGeminiMultimodal._make_client(self)
        _, contents = client._convert_messages([self._audio_message()])
        part = contents[0].parts[1]
        assert part.inline_data is not None
        assert part.inline_data.mime_type == "audio/ogg"
        assert part.inline_data.data == "b64"

    def test_anthropic_rejects_audio(self):
        import pytest

        client = TestAnthropicMultimodal._make_client(self)
        with pytest.raises(ValueError, match="no audio content block"):
            client._convert_messages([self._audio_message()])
