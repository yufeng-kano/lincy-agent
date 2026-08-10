"""Read-only public URL fetch tool backed by httpx."""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from cachetools import TTLCache
from ...llm.schema import Message, ToolDefinition, ToolParameter
from .image import IMAGE_EXTENSIONS_BY_MIME, html_to_markdown

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CHARS = 100_000
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_MIN_MAX_CHARS = 200
_DEFAULT_USER_AGENT = "chat-agent-web-fetch/1.0"

_IMAGE_SAVE_DIR = Path("/tmp/chat-agent-images")

# LRU cache: keyed by URL, TTL 15 minutes, max 50 MB worth of entries (capped by maxsize).
_CACHE_TTL = 15 * 60
_CACHE_MAX_ENTRIES = 256
_url_cache: TTLCache[str, "_CachedResponse"] = TTLCache(
    maxsize=_CACHE_MAX_ENTRIES, ttl=_CACHE_TTL
)

WEB_FETCH_DEFINITION = ToolDefinition(
    name="web_fetch",
    description=(
        "Fetch a specific public URL and extract information using a prompt. "
        "The content is processed by a secondary model that answers your prompt "
        "based on the page content. Results are concise summaries, not raw HTML. "
        "Use this for docs, articles, API responses, and public pages."
    ),
    parameters={
        "url": ToolParameter(
            type="string",
            description="Public http or https URL to fetch.",
        ),
        "prompt": ToolParameter(
            type="string",
            description="What information to extract from the page content.",
        ),
        "max_chars": ToolParameter(
            type="integer",
            description="Optional maximum number of characters to return.",
            json_schema={"minimum": _MIN_MAX_CHARS},
        ),
    },
    required=["url", "prompt"],
)


class _CachedResponse:
    """Lightweight container for cached fetch results."""

    __slots__ = ("body", "final_url", "status_code", "content_type")

    def __init__(
        self, body: bytes, final_url: str, status_code: int, content_type: str
    ) -> None:
        self.body = body
        self.final_url = final_url
        self.status_code = status_code
        self.content_type = content_type


def _truncate_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Bound output size while preserving readability."""
    normalized = text.strip()
    if len(normalized) <= max_chars:
        return normalized, False
    return normalized[:max_chars].rstrip() + "\n\n[Content truncated due to length...]", True


def _extract_charset(content_type: str) -> str | None:
    """Extract a charset token from Content-Type when present."""
    match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def _is_forbidden_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Reject local-only destinations for safety."""
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _validate_public_host(hostname: str) -> str | None:
    """Block obvious SSRF targets before issuing any request."""
    normalized = hostname.strip().lower().rstrip(".")
    if not normalized:
        return "url must include a host."
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith(".local"):
        return "local hosts are not allowed."

    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            resolved = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return None
        for _, _, _, _, sockaddr in resolved:
            if not sockaddr:
                continue
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except ValueError:
                continue
            if _is_forbidden_ip(address):
                return "private or local addresses are not allowed."
        return None

    if _is_forbidden_ip(literal):
        return "private or local addresses are not allowed."
    return None


def _classify_content_type(content_type: str, body: bytes) -> str:
    """Classify the payload into a supported render mode."""
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in {"text/html", "application/xhtml+xml"}:
        return "html"
    if mime == "text/markdown":
        return "markdown"
    if mime == "application/pdf":
        return "pdf"
    if mime in IMAGE_EXTENSIONS_BY_MIME:
        return "image"
    if mime.startswith("text/"):
        return "text"
    if mime == "application/json" or mime.endswith("+json"):
        return "json"
    if mime in {"application/xml", "text/xml"} or mime.endswith("+xml"):
        return "text"
    if mime in {"application/javascript", "text/javascript"}:
        return "text"

    # Fallback: detect via magic bytes.
    sample = body.lstrip()[:64].lower()
    if sample.startswith(b"%pdf"):
        return "pdf"
    if sample.startswith((b"<!doctype html", b"<html")):
        return "html"
    if sample.startswith((b"{", b"[")):
        return "json"
    if _looks_like_text(body):
        return "text"
    return "unknown"


def _looks_like_text(body: bytes) -> bool:
    """Use a small heuristic to avoid decoding obvious binary payloads."""
    sample = body[:1024]
    if not sample:
        return True
    if b"\x00" in sample:
        return False

    allowed_controls = {9, 10, 13}
    printable = 0
    for byte in sample:
        if 32 <= byte <= 126 or byte >= 160 or byte in allowed_controls:
            printable += 1
    return printable / len(sample) >= 0.85


def _decode_body(body: bytes, content_type: str) -> str:
    """Decode text payloads with a small charset fallback chain."""
    encodings = []
    charset = _extract_charset(content_type)
    if charset:
        encodings.append(charset)
    encodings.extend(["utf-8", "utf-16", "latin-1"])

    for encoding in encodings:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _save_fetched_image(body: bytes, content_type: str) -> str:
    """Save fetched image bytes to disk, return the file path."""
    mime = content_type.split(";", 1)[0].strip().lower()
    ext = IMAGE_EXTENSIONS_BY_MIME.get(mime, ".bin")
    _IMAGE_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex[:12]}{ext}"
    path = _IMAGE_SAVE_DIR / filename
    path.write_bytes(body)
    return str(path)


def _render_payload(body: bytes, content_type: str) -> str | None:
    """Render supported payloads into text output.

    Returns ``None`` for content kinds that require special handling
    outside this function (e.g. images saved to disk).
    """
    content_kind = _classify_content_type(content_type, body)

    if content_kind == "pdf":
        from .pdf_utils import extract_pdf_text

        return extract_pdf_text(body)

    if content_kind == "image":
        return None  # handled by caller

    decoded = _decode_body(body, content_type)

    if content_kind == "html":
        return html_to_markdown(decoded)
    if content_kind == "markdown":
        return decoded.strip()
    if content_kind == "json":
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError:
            return decoded.strip()
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    if content_kind == "text":
        return decoded.strip()

    raise ValueError(f"Unsupported content type '{content_type or 'unknown'}'.")


def _format_fetch_result(
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    content_type: str,
    content: str,
    truncated: bool,
) -> str:
    """Format fetch output into a compact text block for the model."""
    lines = [
        f"Fetched: {requested_url}",
        f"Final URL: {final_url}",
        f"Status: {status_code}",
        f"Content-Type: {content_type or 'unknown'}",
    ]
    if truncated:
        lines.append("Truncated: yes")
    lines.append("")
    lines.append(content if content else "(no text extracted)")
    return "\n".join(lines)


def _process_response(
    body: bytes,
    content_type: str,
    *,
    requested_url: str,
    final_url: str,
    status_code: int,
    max_chars: int,
) -> str:
    """Shared render+format logic for both cache-hit and fresh-fetch paths."""
    content_kind = _classify_content_type(content_type, body)

    # Image: save to disk, return path for read_image to pick up.
    if content_kind == "image":
        saved_path = _save_fetched_image(body, content_type)
        return _format_fetch_result(
            requested_url=requested_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type.split(";", 1)[0].strip().lower(),
            content=f"Image saved to: {saved_path}\nUse read_image tool to view this file.",
            truncated=False,
        )

    # Text-like content (HTML, markdown, PDF, JSON, plain text).
    try:
        rendered = _render_payload(body, content_type)
    except ValueError as exc:
        return f"Error: {exc}"

    if rendered is None:
        return f"Error: Unsupported content type '{content_type or 'unknown'}'."

    bounded, truncated = _truncate_text(rendered, max_chars=max_chars)
    return _format_fetch_result(
        requested_url=requested_url,
        final_url=final_url,
        status_code=status_code,
        content_type=content_type.split(";", 1)[0].strip().lower(),
        content=bounded,
        truncated=truncated,
    )


_MAX_SUMMARIZE_CHARS = 200_000  # truncate before sending to summarizer


def _should_skip_summarize(raw: str) -> bool:
    """Skip LLM summarization for error responses and image save results."""
    return raw.startswith("Error") or "Image saved to:" in raw


def _summarize_with_llm(
    content: str,
    prompt: str,
    summarizer: Any,
) -> str:
    """Use a secondary LLM to extract information from fetched content."""
    truncated = content
    if len(truncated) > _MAX_SUMMARIZE_CHARS:
        truncated = (
            truncated[:_MAX_SUMMARIZE_CHARS]
            + "\n\n[Content truncated due to length...]"
        )
    user_msg = (
        f"Web page content:\n---\n{truncated}\n---\n\n"
        f"{prompt}\n\n"
        "Provide a concise response based on the content above. "
        "Include relevant details, code examples, and documentation "
        "excerpts as needed."
    )
    messages = [Message(role="user", content=user_msg)]
    try:
        return summarizer.chat(messages)
    except Exception as exc:
        logger.warning("web_fetch LLM summarization failed: %s", exc)
        return content


def create_web_fetch(
    *,
    timeout: float = 60.0,
    default_max_chars: int = _DEFAULT_MAX_CHARS,
    max_response_chars: int = _DEFAULT_MAX_CHARS,
    max_response_bytes: int = _DEFAULT_MAX_BYTES,
    user_agent: str = _DEFAULT_USER_AGENT,
    allow_private_hosts: bool = False,
    summarizer: Any = None,
):
    """Create an httpx-based web_fetch tool.

    When *summarizer* is provided (an LLM client), fetched content is
    processed by the secondary model using the caller's prompt, returning
    a concise extraction instead of raw page text.
    """

    def web_fetch(
        url: str = "",
        prompt: str = "",
        max_chars: int | None = None,
        **kwargs: Any,
    ) -> str:
        del kwargs
        target = url.strip()
        if not target:
            return "Error: url is required."

        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"}:
            return "Error: url must use http or https."
        if not parsed.hostname:
            return "Error: url must include a host."
        if parsed.username or parsed.password:
            return "Error: url must not include credentials."

        # Auto-upgrade HTTP to HTTPS.
        if parsed.scheme == "http":
            target = "https" + target[4:]

        if max_chars is None:
            effective_max_chars = default_max_chars
        elif not isinstance(max_chars, int) or max_chars < _MIN_MAX_CHARS:
            return f"Error: max_chars must be an integer >= {_MIN_MAX_CHARS}."
        else:
            effective_max_chars = min(max_chars, max_response_chars)

        if not allow_private_hosts:
            host_error = _validate_public_host(parsed.hostname)
            if host_error:
                return f"Error: {host_error}"

        # Check cache first.
        cached = _url_cache.get(target)
        if cached is not None:
            raw = _process_response(
                cached.body,
                cached.content_type,
                requested_url=url.strip(),
                final_url=cached.final_url,
                status_code=cached.status_code,
                max_chars=effective_max_chars,
            )
            if summarizer is not None and prompt and not _should_skip_summarize(raw):
                result = _summarize_with_llm(raw, prompt, summarizer)
                return _truncate_text(result, max_chars=effective_max_chars)[0]
            return raw

        headers = {
            "User-Agent": user_agent,
            "Accept": "text/markdown, text/html, application/json, text/plain;q=0.9, */*;q=0.1",
        }

        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=True,
                headers=headers,
            ) as client:
                with client.stream("GET", target) as response:
                    response.raise_for_status()
                    status_code = response.status_code
                    content_type = response.headers.get("content-type", "")
                    content_length = response.headers.get("content-length")
                    if content_length and content_length.isdigit():
                        if int(content_length) > max_response_bytes:
                            return (
                                "Error: Response too large "
                                f"({content_length} bytes > limit {max_response_bytes})."
                            )

                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > max_response_bytes:
                            return (
                                f"Error: Response exceeded {max_response_bytes} bytes."
                            )
                    final_url = str(response.url)
        except httpx.TimeoutException:
            return "Error: Fetch timed out."
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code if exc.response is not None else None
            return f"Error: Fetch failed ({status})."
        except httpx.HTTPError as exc:
            return f"Error: Fetch failed ({exc})."

        body_bytes = bytes(body)

        # Cache the raw response for future calls.
        _url_cache[target] = _CachedResponse(
            body=body_bytes,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
        )

        raw = _process_response(
            body_bytes,
            content_type,
            requested_url=url.strip(),
            final_url=final_url,
            status_code=status_code,
            max_chars=effective_max_chars,
        )
        if summarizer is not None and prompt and not _should_skip_summarize(raw):
            result = _summarize_with_llm(raw, prompt, summarizer)
            return _truncate_text(result, max_chars=effective_max_chars)[0]
        return raw

    return web_fetch
