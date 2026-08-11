"""Shared transport for project-native LLM proxy clients."""

from __future__ import annotations

from typing import Any

import httpx

from ..schema import raise_if_context_length_error


class NativeProxyClient:
    """Base transport for native proxy endpoints returning a typed response."""

    request_timeout: float

    @staticmethod
    def _get_headers() -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _build_request(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _post(
        self,
        endpoint: str,
        request: Any,
        response_type: type[Any],
        *,
        context_error_patterns: tuple[str, ...],
    ) -> Any:
        with self.httpx.Client(timeout=self.request_timeout) as client:
            response = client.post(
                f"{self.base_url}/{endpoint}",
                headers=self._get_headers(),
                json=request.model_dump(exclude_none=True),
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise_if_context_length_error(exc, patterns=context_error_patterns)
                raise
        return response_type.model_validate(response.json())
