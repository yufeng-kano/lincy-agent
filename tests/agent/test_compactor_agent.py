from lincy.agent.compactor_agent import CompactorAgent
from lincy.llm.schema import Message, ToolCall
from lincy.session.schema import SessionEntry


class _Client:
    def __init__(self, response: str):
        self.response = response
        self.calls = []

    def chat(self, messages, response_schema=None, temperature=None):
        del response_schema, temperature
        self.calls.append(messages)
        return self.response


def test_summarize_returns_stripped_client_response():
    client = _Client("  distilled summary text  ")
    agent = CompactorAgent(client, "sys prompt")

    result = agent.summarize(
        [SessionEntry(message=Message(role="user", content="hello"))]
    )

    assert result == "distilled summary text"
    assert client.calls[0][0].role == "system"
    assert client.calls[0][0].content == "sys prompt"
    assert "hello" in client.calls[0][1].content


def test_summarize_renders_transcript_with_tool_and_user_lines():
    client = _Client("summary")
    agent = CompactorAgent(client, "sys")

    entries = [
        SessionEntry(message=Message(role="user", content="book a flight")),
        SessionEntry(
            message=Message(
                role="assistant",
                content=None,
                tool_calls=[ToolCall(id="1", name="search_flights", arguments={})],
            )
        ),
        SessionEntry(
            message=Message(role="tool", content="found 3 flights", tool_call_id="1", name="search_flights")
        ),
        SessionEntry(message=Message(role="assistant", content="found some options")),
    ]

    agent.summarize(entries)

    transcript = client.calls[0][1].content
    assert "user: book a flight" in transcript
    assert "[tool call: search_flights]" in transcript
    assert "tool (search_flights): found 3 flights" in transcript
    assert "assistant: found some options" in transcript
