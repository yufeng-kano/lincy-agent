"""Tests for agent runtime logging helpers."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from lincy.agent.run_helpers import (
    _debug_print_responder_output,
    _strip_timestamp_prefix,
)
from lincy.llm.schema import LLMResponse


def test_cache_usage_logs_even_when_console_debug_disabled(caplog) -> None:
    console = MagicMock()
    console.debug = False
    response = LLMResponse(
        content="ok",
        tool_calls=[],
        prompt_tokens=100,
        usage_available=True,
        cache_read_tokens=90,
        cache_write_tokens=10,
    )

    with caplog.at_level(logging.INFO):
        _debug_print_responder_output(console, response, label="responder")

    assert any(
        record.getMessage() == "cache: read=90 prompt=100 rate=90% write=10"
        for record in caplog.records
    )
    console.print_debug.assert_not_called()
    console.print_debug_block.assert_not_called()


def test_strip_timestamp_prefix_legacy_single() -> None:
    assert (
        _strip_timestamp_prefix("[2026-07-31 13:35] hello") == "hello"
    )
    assert (
        _strip_timestamp_prefix("[2026-07-31 13:35, now 14:00] hello") == "hello"
    )


def test_strip_timestamp_prefix_weekday_single() -> None:
    assert (
        _strip_timestamp_prefix("[2026-07-31 (Fri) 13:35] hello") == "hello"
    )


def test_strip_timestamp_prefix_stacked_weekday() -> None:
    text = (
        "[2026-07-31 (Fri) 13:39] "
        "[2026-07-31 (Fri) 13:39] "
        "[2026-07-31 (Fri) 13:39] "
        "[2026-07-31 (Fri) 13:38] body"
    )
    assert _strip_timestamp_prefix(text) == "body"


def test_strip_timestamp_prefix_mixed_identical_and_older() -> None:
    text = (
        "[2026-07-31 (Fri) 13:39] "
        "[2026-07-31 (Fri) 13:39] "
        "[2026-07-30 (Thu) 22:01] still here"
    )
    assert _strip_timestamp_prefix(text) == "still here"


def test_strip_timestamp_prefix_no_prefix_or_mid_string() -> None:
    assert _strip_timestamp_prefix("plain message") == "plain message"
    assert (
        _strip_timestamp_prefix("see [2026-07-31 (Fri) 13:35] later")
        == "see [2026-07-31 (Fri) 13:35] later"
    )
    assert (
        _strip_timestamp_prefix("see [2026-07-31 13:35] later")
        == "see [2026-07-31 13:35] later"
    )
