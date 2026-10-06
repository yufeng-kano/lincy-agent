"""Startup stages: validate -> build -> run (web hangs off the server lifespan)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values, find_dotenv
from pydantic import ValidationError

from ..agent.build import BuildError, BuildInputs, BuiltAgent, build_agent
from ..core.config import load_config
from ..core.schema import AppConfig
from ..session import SessionManager
from ..skills import rebuild_personal_skills_index
from ..timezone_utils import configure_runtime_timezone
from ..workspace import WorkspaceInitializer, WorkspaceManager
from ..workspace.people import ensure_user_memory_file, resolve_user_selector
from .check import REPO_ROOT, check_environment
from .errors import BuildFailed, ConfigInvalid, WorkspaceNotReady
from .runtime import HostRuntime
from . import upgrade_notice


@dataclass
class ValidatedEnv:
    config: AppConfig
    agent_os_dir: Path
    user_id: str
    display_name: str
    timezone: str
    resume_id: str | None
    upgrade_message: str


def validate(*, new_session: bool, resume_id: str | None, probe_port: bool = True) -> ValidatedEnv:
    """Check config, workspace and environment; the only write is kernel migration."""
    try:
        config = load_config()
    except (ValidationError, yaml.YAMLError, FileNotFoundError) as e:
        raise ConfigInvalid(f"Config error: {e}") from e
    except SystemExit as e:
        # load_config reports several config errors through SystemExit(message).
        raise ConfigInvalid(str(e.code)) from e

    configure_runtime_timezone(config.app.timezone)

    dotenv_path = find_dotenv(usecwd=True)
    dotenv_user = dotenv_values(dotenv_path).get("CHAT_AGENT_USER") if dotenv_path else None
    user_selector = (dotenv_user or os.environ.get("CHAT_AGENT_USER") or "").strip()
    if not user_selector:
        raise ConfigInvalid("CHAT_AGENT_USER is not set. Add CHAT_AGENT_USER=<name> to .env")

    agent_os_dir = config.get_agent_os_dir()
    workspace = WorkspaceManager(agent_os_dir)
    if not workspace.is_initialized():
        raise WorkspaceNotReady(
            f"Workspace not initialized at {agent_os_dir}. Run: uv run lincy init"
        )

    # Build may depend on prompt files a migration brings in, so it runs here.
    initializer = WorkspaceInitializer(workspace)
    if initializer.needs_upgrade():
        message = initializer.upgrade_kernel().format_startup_message()
        if message:
            # `lincy check` (the upgrade gate) applies the migration but
            # discards its agent; the real start must still deliver the notice.
            upgrade_notice.stash(agent_os_dir, message)
    upgrade_message = upgrade_notice.load(agent_os_dir)

    rebuild_personal_skills_index(agent_os_dir)

    try:
        user_id, display_name = resolve_user_selector(workspace.memory_dir, user_selector)
    except ValueError as e:
        raise WorkspaceNotReady(str(e)) from e
    ensure_user_memory_file(workspace.memory_dir, user_id, display_name)

    check_environment(config, REPO_ROOT, agent_os_dir, probe_port=probe_port)

    if new_session:
        session_choice = None
    elif resume_id is not None:
        session_choice = resume_id
    else:
        recent = SessionManager(agent_os_dir / "session" / "brain").list_recent(
            user_id=user_id, limit=1
        )
        session_choice = recent[0].session_id if recent else None

    return ValidatedEnv(
        config=config,
        agent_os_dir=agent_os_dir,
        user_id=user_id,
        display_name=display_name,
        timezone=config.app.timezone,
        resume_id=session_choice,
        upgrade_message=upgrade_message,
    )


def build(env: ValidatedEnv) -> BuiltAgent:
    try:
        return build_agent(
            BuildInputs(
                config=env.config,
                agent_os_dir=env.agent_os_dir,
                user_id=env.user_id,
                display_name=env.display_name,
                resume_id=env.resume_id,
                upgrade_message=env.upgrade_message,
            )
        )
    except BuildError as e:
        raise BuildFailed(str(e)) from e


def run_start(*, new_session: bool, resume_id: str | None) -> int:
    env = validate(new_session=new_session, resume_id=resume_id)
    built = build(env)
    return HostRuntime(env, built).run()


def run_check() -> int:
    """validate + build, then discard the agent; upgrade uses this as its gate."""
    # The live server holds the port during upgrade, and its session file is
    # being appended to, so check neither probes the port nor loads a session.
    env = validate(new_session=True, resume_id=None, probe_port=False)
    print(f"OK validate (user {env.user_id}, workspace {env.agent_os_dir})", flush=True)
    build(env)
    print("OK build")
    return 0
