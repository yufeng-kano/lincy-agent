"""App-wide timezone utilities.

Call ``configure()`` once at startup with the value from ``config.app.timezone``.
All other code uses ``now()`` / ``get_tz()`` instead of ``datetime.now(utc)``.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
import os
import time
from zoneinfo import ZoneInfo

_UTC_SPEC_RE = re.compile(
    r"^UTC(?:(?P<sign>[+-])(?P<hours>\d{1,2})(?::?(?P<minutes>\d{2}))?)?$"
)

# ------------------------------------------------------------------
# App-wide singleton (set once at startup via configure())
# ------------------------------------------------------------------

_app_tz: tzinfo | None = None
_app_spec: str | None = None


def configure(spec: str) -> None:
    """Set the process-wide timezone from config.app.timezone.

    Must be called exactly once before any call to ``now()`` / ``get_tz()``.
    """
    global _app_tz, _app_spec
    _app_tz = parse_timezone_spec(spec)
    _app_spec = spec


def configure_runtime_timezone(spec: str) -> str:
    """Configure both app-level and process-level timezone state."""
    configure(spec)
    return apply_process_timezone(spec)


def get_tz() -> tzinfo:
    """Return the configured app timezone. Fails fast if not configured."""
    if _app_tz is None:
        raise RuntimeError(
            "timezone not configured; call timezone_utils.configure() at startup"
        )
    return _app_tz


def get_spec() -> str:
    """Return the raw timezone spec string (e.g. 'UTC+8')."""
    if _app_spec is None:
        raise RuntimeError(
            "timezone not configured; call timezone_utils.configure() at startup"
        )
    return _app_spec


def now() -> datetime:
    """Return current time in the configured app timezone."""
    return datetime.now(get_tz())


def localise(dt: datetime) -> datetime:
    """Convert any aware datetime to the app timezone.

    Useful for normalizing deserialized data (old UTC or new local).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(get_tz())


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def format_local_stamp(dt: datetime) -> str:
    """Localise and render the repo's standard prompt timestamp.

    Format: ``%Y-%m-%d (Ddd) %H:%M``, e.g. ``2026-03-12 (Thu) 09:11``.
    """
    local = localise(dt)
    day = _DAY_NAMES[local.weekday()]
    return local.strftime(f"%Y-%m-%d ({day}) %H:%M")


# ------------------------------------------------------------------
# Generic parsing (not tied to the singleton)
# ------------------------------------------------------------------


def parse_timezone_spec(spec: str) -> tzinfo:
    """Parse a timezone spec.

    Supports:
    - Fixed offsets like ``UTC``, ``UTC+8``, ``UTC-05:30``
    - IANA names like ``Asia/Taipei``
    """
    if not isinstance(spec, str):
        raise ValueError("timezone must be a string")

    text = spec.strip()
    if not text:
        raise ValueError("timezone must not be empty")

    match = _UTC_SPEC_RE.fullmatch(text)
    if match:
        sign = match.group("sign")
        if sign is None:
            return timezone.utc

        hours = int(match.group("hours"))
        minutes = int(match.group("minutes") or "0")
        if hours > 23:
            raise ValueError(f"Invalid UTC offset hours in {spec!r}")
        if minutes > 59:
            raise ValueError(f"Invalid UTC offset minutes in {spec!r}")

        offset = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            offset = -offset
        return timezone(offset)

    try:
        return ZoneInfo(text)
    except Exception as exc:
        raise ValueError(
            f"Invalid timezone {spec!r}; use UTC+8, UTC+08:00, or an IANA name like Asia/Taipei"
        ) from exc


def validate_timezone_spec(spec: str) -> str:
    """Validate a timezone spec and return the original input unchanged."""
    parse_timezone_spec(spec)
    return spec


def timezone_spec_to_tz_env(spec: str) -> str:
    """Convert an app timezone spec into a process-level ``TZ`` value."""
    if not isinstance(spec, str):
        raise ValueError("timezone must be a string")

    text = spec.strip()
    match = _UTC_SPEC_RE.fullmatch(text)
    if not match:
        parse_timezone_spec(text)
        return text

    sign = match.group("sign")
    if sign is None:
        return "UTC"

    hours = int(match.group("hours"))
    minutes = int(match.group("minutes") or "0")
    if hours > 23:
        raise ValueError(f"Invalid UTC offset hours in {spec!r}")
    if minutes > 59:
        raise ValueError(f"Invalid UTC offset minutes in {spec!r}")

    reversed_sign = "-" if sign == "+" else "+"
    offset = f"{hours}"
    if minutes:
        offset = f"{offset}:{minutes:02d}"
    return f"<{text}>{reversed_sign}{offset}"


def apply_process_timezone(spec: str) -> str:
    """Set the current process timezone from ``config.app.timezone``."""
    tz_env = timezone_spec_to_tz_env(spec)
    os.environ["TZ"] = tz_env
    tzset = getattr(time, "tzset", None)
    if tzset is not None:
        tzset()
    return tz_env


def format_in_timezone(dt: datetime, timezone_spec: str, fmt: str) -> str:
    """Format a datetime in the configured timezone.

    Naive datetimes are treated as UTC to keep behavior deterministic.
    """
    tz = parse_timezone_spec(timezone_spec)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime(fmt)
