"""Shared macOS app bridge runtime."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from ...security import resolve_allowed_path
from .runtime import _SLOW_APP_TOOL_SECONDS, _format_app_tool_log_details, logger

class _BridgeBase:
    def __init__(
        self,
        *,
        base_dir: Path,
        allowed_paths: list[str],
        timeout_seconds: float,
        max_search_results: int,
        photos_export_dir: str,
        mail_export_dir: str = "tmp/mail-attachments",
        vision_agent: Any | None = None,
        notes_summarizer: Any | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._allowed_paths = allowed_paths
        self._timeout_seconds = timeout_seconds
        self._max_search_results = max_search_results
        self._photos_export_dir = photos_export_dir
        self._mail_export_dir = mail_export_dir
        self._vision_agent = vision_agent
        self._notes_summarizer = notes_summarizer
        self._apple_notes_cache_dir = self._base_dir / "cache" / "apple_notes"
        self._apple_notes_cache_dir.mkdir(parents=True, exist_ok=True)

    def _prepare_export_dir(
        self,
        destination_dir: str | None,
        configured_subdir: str | None = None,
    ) -> Path:
        """Resolve and validate an app export directory."""
        configured_subdir = configured_subdir or self._photos_export_dir
        candidate = Path(destination_dir) if destination_dir else (
            self._base_dir / configured_subdir / datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        target = resolve_allowed_path(str(candidate), self._allowed_paths, self._base_dir)
        if target is None:
            raise ValueError(f"destination_dir is outside allowed paths: {candidate}")
        return target

    def _prepare_mail_export_dir(self, destination_dir: str | None) -> Path:
        """Prepare a Mail attachment export directory."""
        return self._prepare_export_dir(destination_dir, self._mail_export_dir)

    def _run_jxa_json(
        self,
        body: str,
        *,
        payload: dict[str, Any] | None = None,
        operation: str | None = None,
        log_details: dict[str, Any] | None = None,
        helpers: str = "",
    ) -> dict[str, Any]:
        """Run a JXA script and parse JSON output."""
        script = f"""
    ObjC.import("stdlib");
    function readPayload() {{
      const raw = $.getenv("CHAT_AGENT_APP_TOOL_PAYLOAD");
      return raw ? JSON.parse(ObjC.unwrap(raw)) : {{}};
    }}
    function iso(value) {{
      if (!value) {{
    return null;
      }}
      try {{
    return value.toISOString();
      }} catch (error) {{
    return null;
      }}
    }}
    function lower(value) {{
      return (value || "").toString().toLowerCase();
    }}
    function valueOrNull(value) {{
      return value === undefined ? null : value;
    }}
    function safe(fn, fallback) {{
      try {{
    const value = fn();
    return value === undefined ? fallback : value;
      }} catch (error) {{
    return fallback;
      }}
    }}
    function clampLimit(value, maxLimit) {{
      const raw = value || maxLimit;
      return Math.max(1, Math.min(raw, maxLimit));
    }}
    function clampScanLimit(value, defaultLimit, maxLimit) {{
      const raw = value || defaultLimit;
      return Math.max(1, Math.min(Number(raw), maxLimit));
    }}
    function compareIsoAsc(a, b) {{
      if (!a && !b) return 0;
      if (!a) return 1;
      if (!b) return -1;
      return new Date(a) - new Date(b);
    }}
    function compareIsoDesc(a, b) {{
      return compareIsoAsc(b, a);
    }}
    function compareTextAsc(a, b) {{
      return (a || "").localeCompare(b || "");
    }}
    {helpers}
    function main() {{
    {body}
    }}
    JSON.stringify(main());
    """
        env = os.environ.copy()
        env["CHAT_AGENT_APP_TOOL_PAYLOAD"] = json.dumps(payload or {})
        started = time.monotonic()
        try:
            completed = subprocess.run(
                ["osascript", "-l", "JavaScript"],
                input=script,
                text=True,
                capture_output=True,
                env=env,
                timeout=self._timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            logger.warning(
                "macOS app tool timeout engine=jxa operation=%s elapsed=%.2fs details=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details or payload),
            )
            raise RuntimeError(
                f"{operation or 'macOS app tool'} timed out after {self._timeout_seconds:.1f} seconds"
            ) from exc
        elapsed = time.monotonic() - started
        if elapsed >= _SLOW_APP_TOOL_SECONDS:
            logger.warning(
                "macOS app tool slow engine=jxa operation=%s elapsed=%.2fs details=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details or payload),
            )
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout).strip()
            logger.warning(
                "macOS app tool failure engine=jxa operation=%s elapsed=%.2fs details=%s error=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details or payload),
                stderr or "JXA command failed",
            )
            raise RuntimeError(stderr or "JXA command failed")
        output = completed.stdout.strip()
        if not output:
            logger.warning(
                "macOS app tool empty-output engine=jxa operation=%s elapsed=%.2fs details=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details or payload),
            )
            raise RuntimeError("JXA command returned no output")
        return json.loads(output)

    def _run_applescript(
        self,
        script: str,
        *,
        env: dict[str, str] | None = None,
        utf8_files: dict[str, str] | None = None,
        operation: str | None = None,
        log_details: dict[str, Any] | None = None,
    ) -> str:
        """Run an AppleScript snippet and return stdout."""
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)
        started = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="chat-agent-osascript-") as temp_dir:
                if utf8_files:
                    script = (
                        """
    on readUtf8EnvFile(envName)
      set filePath to system attribute (envName & "_FILE")
      try
    return read (POSIX file filePath) as «class utf8»
      on error number -39
    return ""
      end try
    end readUtf8EnvFile

    """
                        + script
                    )
                    temp_root = Path(temp_dir)
                    for key, value in utf8_files.items():
                        path = temp_root / f"{key}.txt"
                        path.write_text(value, encoding="utf-8")
                        merged_env[f"{key}_FILE"] = str(path)
                completed = subprocess.run(
                    ["osascript"],
                    input=script,
                    text=True,
                    capture_output=True,
                    env=merged_env,
                    timeout=self._timeout_seconds,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            logger.warning(
                "macOS app tool timeout engine=applescript operation=%s elapsed=%.2fs details=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details),
            )
            raise RuntimeError(
                f"{operation or 'macOS app tool'} timed out after {self._timeout_seconds:.1f} seconds"
            ) from exc
        elapsed = time.monotonic() - started
        if elapsed >= _SLOW_APP_TOOL_SECONDS:
            logger.warning(
                "macOS app tool slow engine=applescript operation=%s elapsed=%.2fs details=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details),
            )
        if completed.returncode != 0:
            stderr = (completed.stderr or completed.stdout).strip()
            logger.warning(
                "macOS app tool failure engine=applescript operation=%s elapsed=%.2fs details=%s error=%s",
                operation or "unknown",
                elapsed,
                _format_app_tool_log_details(log_details),
                stderr or "AppleScript command failed",
            )
            raise RuntimeError(stderr or "AppleScript command failed")
        return completed.stdout.strip()

