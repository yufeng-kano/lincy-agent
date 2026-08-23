"""Asyncio-based scheduler for periodic restarts and crash recovery."""

import asyncio
import logging

from .config import load_supervisor_config
from .process import ManagedProcess, ProcessState, topological_sort
from .schema import SupervisorConfig
from .upgrade import has_remote_changes, pull_and_post, self_restart, snapshot_watch_paths

logger = logging.getLogger(__name__)

_CRASH_CHECK_INTERVAL = 5  # seconds


class ProcessStartupError(RuntimeError):
    """Raised when a managed process fails during ordered startup."""


class Scheduler:
    """Manages process lifecycle with periodic restart cycles."""

    def __init__(
        self,
        config: SupervisorConfig,
        processes: dict[str, ManagedProcess],
        *,
        config_path: str = "supervisor.yaml",
    ):
        self._config = config
        self._processes = processes
        self._config_path = config_path
        self._startup_order = topological_sort(config.processes)
        self._shutdown_order = list(reversed(self._startup_order))
        self._running = False
        self._cycling = False

    def cleanup_stale(self) -> None:
        """Kill leftover processes from a previous supervisor run."""
        for name in self._startup_order:
            if name not in self._processes:
                continue
            self._processes[name].cleanup_stale()

    async def start_all(self) -> None:
        """Start all enabled processes in dependency order.

        If a process has health_check_url configured, waits until the
        endpoint returns 200 before starting the next process.
        """
        for name in self._startup_order:
            if name not in self._processes:
                continue
            proc = self._processes[name]
            logger.info("Starting %s...", name)
            await proc.start()
            if proc.state == ProcessState.CRASHED:
                raise ProcessStartupError(
                    f"{name} exited during startup with code {proc.returncode}"
                )
            if not await proc.wait_healthy():
                raise ProcessStartupError(f"{name} failed health check")

    async def stop_all(self) -> None:
        """Stop all processes in reverse dependency order."""
        for name in self._shutdown_order:
            if name not in self._processes:
                continue
            proc = self._processes[name]
            if proc.state in (ProcessState.RUNNING, ProcessState.STARTING):
                logger.info("Stopping %s...", name)
                await proc.stop()

    async def restart_cycle(self) -> None:
        """Perform a periodic restart cycle.

        1. Stop processes that have join_restart_cycle (reverse dep order)
        2. Stop all remaining running processes (reverse dep order)
        3. Start all (forward dep order)
        """
        if self._cycling:
            logger.warning("Restart cycle already in progress, skipping")
            return

        self._cycling = True
        try:
            # Stop cycle participants first (e.g. chat-cli before its dependencies)
            for name in self._shutdown_order:
                if name not in self._processes:
                    continue
                proc = self._processes[name]
                if proc.config.join_restart_cycle and proc.state != ProcessState.STOPPED:
                    logger.info("Cycle: stopping %s...", name)
                    await proc.stop()

            # Stop remaining
            for name in self._shutdown_order:
                if name not in self._processes:
                    continue
                proc = self._processes[name]
                if proc.state != ProcessState.STOPPED:
                    logger.info("Cycle: stopping %s...", name)
                    await proc.stop()

            # Reset crash counts (intentional cycle, not a crash)
            for proc in self._processes.values():
                proc.reset_crash_count()

            # Start all
            await self.start_all()
            logger.info("Restart cycle completed")
        finally:
            self._cycling = False

    async def _check_and_upgrade(self) -> None:
        """Check for remote changes and auto-upgrade if found."""
        upgrade_cfg = self._config.upgrade
        branch = upgrade_cfg.branch

        if not has_remote_changes(branch):
            return

        logger.info("=== Auto-upgrade started ===")

        watch_before = snapshot_watch_paths(upgrade_cfg.self_watch_paths)
        logger.info("Snapshot before: %d watched files", len(watch_before))

        ok, err = pull_and_post(upgrade_cfg)
        if not ok:
            logger.error("Auto-upgrade aborted: %s", err)
            return

        reloaded_config = load_supervisor_config(self._config_path)
        if _supervisor_config_changed(self._config, reloaded_config):
            logger.info(
                "Supervisor config changed after pull; self-restarting to reload process graph"
            )
            self_restart()
            return

        logger.info("Pull succeeded, restarting processes...")
        await self.restart_cycle()

        watch_after = snapshot_watch_paths(upgrade_cfg.self_watch_paths)
        changed = {k for k in watch_after if watch_before.get(k) != watch_after[k]}
        added = set(watch_after) - set(watch_before)
        if changed or added:
            logger.info(
                "Supervisor code changed (%d modified, %d added); self-restarting",
                len(changed), len(added),
            )
            self_restart()

        logger.info("=== Auto-upgrade completed ===")

    async def run(self) -> None:
        """Main scheduler loop: crash detection + periodic restarts + auto-upgrade."""
        self._running = True
        interval = self._config.restart.interval_hours
        restart_interval_sec = (
            interval * 3600
            if self._config.restart.enabled and interval
            else None
        )
        restart_elapsed = 0.0

        upgrade_cfg = self._config.upgrade
        upgrade_interval_sec = (
            upgrade_cfg.check_interval_minutes * 60
            if upgrade_cfg.auto_check
            else None
        )
        upgrade_elapsed = 0.0

        while self._running:
            await asyncio.sleep(_CRASH_CHECK_INTERVAL)
            restart_elapsed += _CRASH_CHECK_INTERVAL
            upgrade_elapsed += _CRASH_CHECK_INTERVAL

            # Crash detection + auto-restart (with backoff)
            for name, proc in self._processes.items():
                if proc.detect_crash() and proc.config.auto_restart:
                    if proc.should_restart():
                        logger.info("Auto-restarting %s...", name)
                        await proc.start()

            # Periodic restart cycle
            if restart_interval_sec and restart_elapsed >= restart_interval_sec:
                logger.info("Periodic restart cycle triggered")
                await self.restart_cycle()
                restart_elapsed = 0.0

            # Auto-upgrade check
            if upgrade_interval_sec and upgrade_elapsed >= upgrade_interval_sec:
                await self._check_and_upgrade()
                upgrade_elapsed = 0.0

    def request_stop(self) -> None:
        """Signal the scheduler to stop."""
        self._running = False


def _supervisor_config_changed(
    current: SupervisorConfig,
    updated: SupervisorConfig,
) -> bool:
    """Return True when the effective supervisor config changed after pull."""
    return current.model_dump() != updated.model_dump()
