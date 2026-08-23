"""Pydantic schemas for supervisor configuration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictModel):
    """Supervisor API server bind settings."""

    host: str = "127.0.0.1"
    port: int = Field(default=9000, ge=1, le=65535)


class RestartConfig(StrictModel):
    """Periodic restart cycle configuration."""

    enabled: bool = False
    interval_hours: int | None = Field(default=None, ge=1)


class ProcessConfig(StrictModel):
    """Configuration for one managed process."""

    enabled: bool = True
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    auto_restart: bool = True
    startup_delay: float = Field(default=0.0, ge=0)
    control_url: str | None = None
    shutdown_timeout: float = Field(default=30.0, ge=1)
    join_restart_cycle: bool = False
    depends_on: list[str] = Field(default_factory=list)
    log_output: bool = False
    start_new_session: bool = True
    health_check_url: str | None = None
    health_check_timeout: float = Field(default=30.0, ge=1)
    health_check_interval: float = Field(default=1.0, ge=0.1)
    auto_enable_when_any_agent_uses_provider: Literal[
        "deepseek",
        "anthropic",
        "openai",
        "gemini",
        "openrouter",
        "litellm",
        "ollama_native",
    ] | None = None


class UpgradeConfig(StrictModel):
    """Upgrade flow configuration."""

    auto_check: bool = False
    check_interval_minutes: int = Field(default=30, ge=1)
    branch: str = "main"
    post_pull: list[str] = Field(default_factory=list)
    self_watch_paths: list[str] = Field(default_factory=list)


class SupervisorConfig(StrictModel):
    """Root supervisor configuration."""

    server: ServerConfig = Field(default_factory=ServerConfig)
    restart: RestartConfig = Field(default_factory=RestartConfig)
    processes: dict[str, ProcessConfig] = Field(default_factory=dict)
    upgrade: UpgradeConfig = Field(default_factory=UpgradeConfig)
