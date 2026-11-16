"""Tests for m0189: enable the recall prompt cache in workspace-level configs."""

from pathlib import Path

import yaml

from lincy.workspace.migrations.m0189_recall_worker_tool_result_cache import (
    M0189RecallWorkerToolResultCache,
)


def _upgrade(tmp_path: Path, config: dict, rel: str = "agent.yaml") -> dict:
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir(exist_ok=True)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    M0189RecallWorkerToolResultCache().upgrade(kernel_dir, tmp_path / "templates")

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_adds_recall_cache_to_both_config_locations(tmp_path: Path):
    config = {"agents": {"recall": {"enabled": True, "llm": "cfgs/llm/kano-proxy/recall.yaml"}}}

    for rel in ("agent.yaml", "cfgs/agent.yaml"):
        upgraded = _upgrade(tmp_path, config, rel)
        assert upgraded["agents"]["recall"]["cache"] == {"enabled": True, "ttl": "ephemeral"}
        assert upgraded["agents"]["recall"]["llm"] == "cfgs/llm/kano-proxy/recall.yaml"

    assert M0189RecallWorkerToolResultCache.version == "0.87.1"


def test_keeps_an_existing_recall_cache_setting(tmp_path: Path):
    config = {"agents": {"recall": {"enabled": True, "cache": {"enabled": False, "ttl": "1h"}}}}

    upgraded = _upgrade(tmp_path, config)

    assert upgraded["agents"]["recall"]["cache"] == {"enabled": False, "ttl": "1h"}


def test_leaves_configs_without_a_recall_agent_untouched(tmp_path: Path):
    config = {"agents": {"brain": {"llm": "cfgs/llm/kano-proxy/brain.yaml"}}}

    assert _upgrade(tmp_path, config) == config


def test_runs_without_any_workspace_config(tmp_path: Path):
    kernel_dir = tmp_path / "kernel"
    kernel_dir.mkdir()

    M0189RecallWorkerToolResultCache().upgrade(kernel_dir, tmp_path / "templates")
