"""Shared test fakes for OpenAI-compatible providers."""


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200, headers: dict[str, str] | None = None):
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "http://fake/v1/chat/completions")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=httpx.Response(
                    self.status_code,
                    request=request,
                    text=self._text(),
                ),
            )

    @property
    def text(self) -> str:
        return self._text()

    def _text(self) -> str:
        import json

        return json.dumps(self.payload)

    def json(self) -> dict:
        return self.payload


class FakeHttpxClient:
    """Fake httpx.Client that records calls and replays effects."""

    def __init__(self, effects: list, calls: list[dict]):
        self.effects = effects
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, headers: dict, json: dict) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "json": json})
        effect = self.effects.pop(0)
        if isinstance(effect, Exception):
            raise effect
        if isinstance(effect, FakeResponse):
            return effect
        return FakeResponse(effect)


def make_openai_payload(content: str = "ok") -> dict:
    return {"choices": [{"message": {"content": content}}]}
