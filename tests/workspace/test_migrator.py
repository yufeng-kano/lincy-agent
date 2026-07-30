"""Tests for Migrator and migration system."""

from importlib import resources
import pytest
from pathlib import Path

import yaml

from lincy.workspace.migrator import Migrator, _parse_version, KERNEL_VERSION
from lincy.workspace.migrations.base import Migration


class StubMigration(Migration):
    """Test migration that creates a marker file."""

    version = "0.9.0"

    def upgrade(self, kernel_dir: Path, templates_dir: Path) -> None:
        (kernel_dir / f"migrated-{self.version}").touch()


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("0.1.3") == (0, 1, 3)

    def test_comparison(self):
        assert _parse_version("0.2.0") > _parse_version("0.1.3")

    def test_comparison_double_digit(self):
        """0.1.10 > 0.1.9 must be correct (string comparison would fail)."""
        assert _parse_version("0.1.10") > _parse_version("0.1.9")


class TestKernelVersion:
    def test_derived_from_migrations(self):
        """KERNEL_VERSION matches the last migration's version."""
        from lincy.workspace.migrations import ALL_MIGRATIONS

        assert KERNEL_VERSION == ALL_MIGRATIONS[-1].version

    def test_matches_template_kernel_info_version(self):
        """Template kernel/info.yaml version must stay in sync with migrations."""
        templates_dir = Path(str(resources.files("lincy.workspace"))) / "templates"
        with open(templates_dir / "kernel" / "info.yaml") as f:
            info = yaml.safe_load(f)
        assert info["version"] == KERNEL_VERSION


class TestMigrator:
    @pytest.fixture
    def kernel_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "kernel"
        d.mkdir()
        return d

    @pytest.fixture
    def templates_dir(self, tmp_path: Path) -> Path:
        d = tmp_path / "templates"
        d.mkdir()
        return d

    def _write_info(self, kernel_dir: Path, version: str) -> None:
        with open(kernel_dir / "info.yaml", "w") as f:
            yaml.dump({"version": version}, f)

    def test_get_current_version(self, kernel_dir, templates_dir):
        self._write_info(kernel_dir, "1.2.3")
        m = Migrator(kernel_dir, templates_dir)
        assert m.get_current_version() == "1.2.3"

    def test_get_current_version_missing(self, kernel_dir, templates_dir):
        (kernel_dir / "info.yaml").unlink(missing_ok=True)
        m = Migrator(kernel_dir, templates_dir)
        assert m.get_current_version() == "0.0.0"

    def test_get_pending_none(self, kernel_dir, templates_dir):
        """No pending when at latest version."""
        self._write_info(kernel_dir, KERNEL_VERSION)
        m = Migrator(kernel_dir, templates_dir)
        assert m.get_pending_migrations() == []

    def test_needs_migration_false(self, kernel_dir, templates_dir):
        self._write_info(kernel_dir, KERNEL_VERSION)
        m = Migrator(kernel_dir, templates_dir)
        assert m.needs_migration() is False

    def test_needs_migration_true(self, kernel_dir, templates_dir):
        self._write_info(kernel_dir, "0.0.1")
        m = Migrator(kernel_dir, templates_dir)
        assert m.needs_migration() is True

    def test_run_migrations(self, kernel_dir, templates_dir):
        """Migrations run and version is updated."""
        self._write_info(kernel_dir, "0.0.1")
        m = Migrator(kernel_dir, templates_dir)
        result = m.run_migrations()

        assert result.upgraded
        assert len(result.applied_versions) > 0
        assert m.get_current_version() == KERNEL_VERSION
        assert m.needs_migration() is False

    def test_run_migrations_none_pending(self, kernel_dir, templates_dir):
        """No-op when already at latest version."""
        self._write_info(kernel_dir, KERNEL_VERSION)
        m = Migrator(kernel_dir, templates_dir)
        result = m.run_migrations()
        assert not result.upgraded

    def test_update_version_persists(self, kernel_dir, templates_dir):
        """_update_version writes to info.yaml."""
        self._write_info(kernel_dir, "0.0.1")
        m = Migrator(kernel_dir, templates_dir)
        m._update_version("9.9.9")

        with open(kernel_dir / "info.yaml") as f:
            info = yaml.safe_load(f)
        assert info["version"] == "9.9.9"

    def test_run_migrations_removes_timezone_from_info_yaml(self, kernel_dir, templates_dir):
        with open(kernel_dir / "info.yaml", "w") as f:
            yaml.dump(
                {
                    # Start at pre-m0090 version so only the timezone-removal
                    # migration is pending in this unit test.
                    "version": "0.54.0",
                    "updated": "2026-02-21",
                    "timezone": "Asia/Taipei",
                    "custom": "keep-me",
                },
                f,
            )

        m = Migrator(kernel_dir, templates_dir)
        result = m.run_migrations()

        assert result.upgraded
        with open(kernel_dir / "info.yaml") as f:
            info = yaml.safe_load(f)
        assert info["version"] == KERNEL_VERSION
        assert info["custom"] == "keep-me"
        assert "timezone" not in info

    def test_run_migrations_moves_legacy_personal_skills_out_of_memory(
        self,
        tmp_path: Path,
    ):
        kernel_dir = tmp_path / "kernel"
        kernel_dir.mkdir()
        (kernel_dir / "info.yaml").write_text("version: '0.69.1'", encoding="utf-8")

        legacy_skill = tmp_path / "memory" / "agent" / "skills" / "demo-skill"
        legacy_skill.mkdir(parents=True)
        (legacy_skill / "SKILL.md").write_text(
            "---\n"
            "name: demo-skill\n"
            'description: "Demo migration skill."\n'
            "---\n\n"
            "# Demo\n",
            encoding="utf-8",
        )
        agent_index = tmp_path / "memory" / "agent" / "index.md"
        agent_index.parent.mkdir(parents=True, exist_ok=True)
        agent_index.write_text(
            "# Agent 記憶索引\n\n"
            "- [persona.md](persona.md) — 核心身份與人格\n"
            "- [skills/](skills/) — 發展的能力\n",
            encoding="utf-8",
        )

        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path)
        init = WorkspaceInitializer(manager)
        templates_dir = init._get_templates_dir() / "kernel"

        m = Migrator(kernel_dir, templates_dir)
        result = m.run_migrations()

        assert "0.70.0" in result.applied_versions
        assert not (tmp_path / "memory" / "agent" / "skills").exists()
        assert (tmp_path / "personal-skills" / "demo-skill" / "SKILL.md").exists()
        assert not (tmp_path / "personal-skills" / "memory-maintenance").exists()
        assert (kernel_dir / "builtin-skills" / "memory-maintenance" / "SKILL.md").exists()

        personal_index = (tmp_path / "personal-skills" / "index.md").read_text(
            encoding="utf-8"
        )
        assert "[demo-skill]" in personal_index
        assert "Demo migration skill." in personal_index

        updated_agent_index = agent_index.read_text(encoding="utf-8")
        assert "[skills/](skills/)" not in updated_agent_index


class TestM0002AgentsStructure:
    """Tests for the agents/ directory restructure migration."""

    def test_removes_old_system_prompts(self, tmp_path: Path):
        """M0002 removes system-prompts/ directory."""
        kernel_dir = tmp_path / "kernel"
        kernel_dir.mkdir()
        (kernel_dir / "info.yaml").write_text("version: '0.1.3'")
        old_dir = kernel_dir / "system-prompts"
        old_dir.mkdir()
        (old_dir / "brain.md").write_text("old prompt")

        # Use real templates
        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path)
        init = WorkspaceInitializer(manager)
        templates_dir = init._get_templates_dir() / "kernel"

        from lincy.workspace.migrations.m0002_agents_structure import M0002AgentsStructure

        m = M0002AgentsStructure()
        m.upgrade(kernel_dir, templates_dir)

        assert not old_dir.exists()

    def test_copies_agents_structure(self, tmp_path: Path):
        """M0002 copies agents/ from templates."""
        kernel_dir = tmp_path / "kernel"
        kernel_dir.mkdir()
        (kernel_dir / "info.yaml").write_text("version: '0.1.3'")

        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path)
        init = WorkspaceInitializer(manager)
        templates_dir = init._get_templates_dir() / "kernel"

        from lincy.workspace.migrations.m0002_agents_structure import M0002AgentsStructure

        m = M0002AgentsStructure()
        m.upgrade(kernel_dir, templates_dir)

        assert (kernel_dir / "agents" / "brain" / "prompts" / "system.md").exists()
        assert (kernel_dir / "agents" / "init" / "prompts" / "system.md").exists()

    def test_full_migration_chain(self, tmp_path: Path):
        """Full upgrade from 0.1.3 to latest via migrator."""
        kernel_dir = tmp_path / "kernel"
        kernel_dir.mkdir()
        # Simulate old workspace
        old_dir = kernel_dir / "system-prompts"
        old_dir.mkdir()
        (old_dir / "brain.md").write_text("old")
        (kernel_dir / "info.yaml").write_text("version: '0.1.3'")

        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager

        manager = WorkspaceManager(tmp_path)
        init = WorkspaceInitializer(manager)
        templates_dir = init._get_templates_dir() / "kernel"

        m = Migrator(kernel_dir, templates_dir)
        result = m.run_migrations()

        assert "0.2.0" in result.applied_versions
        assert m.get_current_version() == KERNEL_VERSION
        assert not old_dir.exists()
        assert (kernel_dir / "agents" / "brain" / "prompts" / "system.md").exists()


class TestM0006ReviewerAgents:
    """Tests for reviewer prompt split migration."""

    def test_moves_existing_reviewer_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        old_prompts = kernel_dir / "agents" / "brain" / "prompts"
        old_prompts.mkdir(parents=True)
        (old_prompts / "reviewer-pre.md").write_text("custom pre reviewer prompt")
        (old_prompts / "reviewer-post.md").write_text("custom post reviewer prompt")

        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager
        from lincy.workspace.migrations.m0006_reviewer_agents import (
            M0006ReviewerAgents,
        )

        manager = WorkspaceManager(tmp_path)
        templates_dir = WorkspaceInitializer(manager)._get_templates_dir() / "kernel"

        migration = M0006ReviewerAgents()
        migration.upgrade(kernel_dir, templates_dir)

        pre_path = kernel_dir / "agents" / "pre_reviewer" / "prompts" / "system.md"
        post_path = kernel_dir / "agents" / "post_reviewer" / "prompts" / "system.md"
        assert pre_path.exists()
        assert post_path.exists()
        assert pre_path.read_text() == "custom pre reviewer prompt"
        assert post_path.read_text() == "custom post reviewer prompt"
        assert not (old_prompts / "reviewer-pre.md").exists()
        assert not (old_prompts / "reviewer-post.md").exists()

    def test_copies_template_when_old_prompt_missing(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        kernel_dir.mkdir()

        from lincy.workspace.initializer import WorkspaceInitializer
        from lincy.workspace import WorkspaceManager
        from lincy.workspace.migrations.m0006_reviewer_agents import (
            M0006ReviewerAgents,
        )

        manager = WorkspaceManager(tmp_path)
        templates_dir = WorkspaceInitializer(manager)._get_templates_dir() / "kernel"

        migration = M0006ReviewerAgents()
        migration.upgrade(kernel_dir, templates_dir)

        # Reviewer templates have been removed; migration skips gracefully
        post_path = kernel_dir / "agents" / "post_reviewer" / "prompts" / "system.md"
        assert not post_path.exists()


class TestM0007PostReviewerPromptTuning:
    """Tests for post reviewer prompt tuning migration."""

    def test_overwrites_post_reviewer_prompt_from_template(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        dst = kernel_dir / "agents" / "post_reviewer" / "prompts"
        dst.mkdir(parents=True)
        (dst / "system.md").write_text("old prompt")

        templates_dir = tmp_path / "templates"
        src = templates_dir / "agents" / "post_reviewer" / "prompts"
        src.mkdir(parents=True)
        (src / "system.md").write_text("new tuned prompt")

        from lincy.workspace.migrations.m0007_post_reviewer_prompt_tuning import (
            M0007PostReviewerPromptTuning,
        )

        migration = M0007PostReviewerPromptTuning()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "new tuned prompt"


class TestM0030StrictTargetAnomalySignals:
    """Tests for strict target/anomaly prompt migration."""

    def test_copies_brain_and_post_reviewer_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"
        (kernel_dir / "agents" / "brain" / "prompts").mkdir(parents=True)
        (kernel_dir / "agents" / "post_reviewer" / "prompts").mkdir(parents=True)
        (kernel_dir / "agents" / "shutdown_reviewer" / "prompts").mkdir(parents=True)
        (templates_dir / "agents" / "brain" / "prompts").mkdir(parents=True)
        (templates_dir / "agents" / "post_reviewer" / "prompts").mkdir(parents=True)
        (templates_dir / "agents" / "shutdown_reviewer" / "prompts").mkdir(parents=True)

        (templates_dir / "agents" / "brain" / "prompts" / "system.md").write_text(
            "brain strict v0.9.0"
        )
        (templates_dir / "agents" / "brain" / "prompts" / "shutdown.md").write_text(
            "brain shutdown strict v0.9.0"
        )
        (templates_dir / "agents" / "post_reviewer" / "prompts" / "system.md").write_text(
            "post reviewer strict v0.9.0"
        )
        (templates_dir / "agents" / "post_reviewer" / "prompts" / "parse-retry.md").write_text(
            "parse retry strict v0.9.0"
        )
        (templates_dir / "agents" / "shutdown_reviewer" / "prompts" / "system.md").write_text(
            "shutdown reviewer strict v0.9.0"
        )
        (templates_dir / "agents" / "shutdown_reviewer" / "prompts" / "parse-retry.md").write_text(
            "shutdown parse retry strict v0.9.0"
        )

        from lincy.workspace.migrations.m0030_strict_target_anomaly_signals import (
            M0030StrictTargetAnomalySignals,
        )

        migration = M0030StrictTargetAnomalySignals()
        migration.upgrade(kernel_dir, templates_dir)

        assert (kernel_dir / "agents" / "brain" / "prompts" / "system.md").read_text() == (
            "brain strict v0.9.0"
        )
        assert (
            kernel_dir / "agents" / "brain" / "prompts" / "shutdown.md"
        ).read_text() == "brain shutdown strict v0.9.0"
        assert (
            kernel_dir / "agents" / "post_reviewer" / "prompts" / "system.md"
        ).read_text() == "post reviewer strict v0.9.0"
        assert (
            kernel_dir / "agents" / "post_reviewer" / "prompts" / "parse-retry.md"
        ).read_text() == "parse retry strict v0.9.0"
        assert (
            kernel_dir / "agents" / "shutdown_reviewer" / "prompts" / "system.md"
        ).read_text() == "shutdown reviewer strict v0.9.0"
        assert (
            kernel_dir / "agents" / "shutdown_reviewer" / "prompts" / "parse-retry.md"
        ).read_text() == "shutdown parse retry strict v0.9.0"


class TestM0031MemorySearchTwoStageConfigurableLimits:
    """Tests for memory_searcher prompt refresh migration."""

    def test_copies_memory_searcher_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"
        (kernel_dir / "agents" / "memory_searcher" / "prompts").mkdir(parents=True)
        (templates_dir / "agents" / "memory_searcher" / "prompts").mkdir(parents=True)

        (templates_dir / "agents" / "memory_searcher" / "prompts" / "system.md").write_text(
            "memory searcher two-stage v0.9.1"
        )
        (templates_dir / "agents" / "memory_searcher" / "prompts" / "parse-retry.md").write_text(
            "memory searcher parse retry v0.9.1"
        )

        from lincy.workspace.migrations.m0031_memory_search_two_stage_configurable_limits import (
            M0031MemorySearchTwoStageConfigurableLimits,
        )

        migration = M0031MemorySearchTwoStageConfigurableLimits()
        migration.upgrade(kernel_dir, templates_dir)

        assert (
            kernel_dir / "agents" / "memory_searcher" / "prompts" / "system.md"
        ).read_text() == "memory searcher two-stage v0.9.1"
        assert (
            kernel_dir / "agents" / "memory_searcher" / "prompts" / "parse-retry.md"
        ).read_text() == "memory searcher parse retry v0.9.1"


class TestM0126RemoveMemorySearcher:
    """Tests for memory_searcher prompt cleanup migration."""

    def test_removes_memory_searcher_prompt_directory(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"
        prompt_dir = kernel_dir / "agents" / "memory_searcher" / "prompts"
        prompt_dir.mkdir(parents=True)
        (prompt_dir / "system.md").write_text("legacy prompt")

        from lincy.workspace.migrations.m0126_remove_memory_searcher import (
            M0126RemoveMemorySearcher,
        )

        migration = M0126RemoveMemorySearcher()
        migration.upgrade(kernel_dir, templates_dir)

        assert not (kernel_dir / "agents" / "memory_searcher").exists()


class TestM0008PostReviewerStructuredActions:
    """Tests for structured action post-review prompt migration."""

    def test_overwrites_post_reviewer_prompt_from_template(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        dst = kernel_dir / "agents" / "post_reviewer" / "prompts"
        dst.mkdir(parents=True)
        (dst / "system.md").write_text("old prompt")

        templates_dir = tmp_path / "templates"
        src = templates_dir / "agents" / "post_reviewer" / "prompts"
        src.mkdir(parents=True)
        (src / "system.md").write_text("new structured actions prompt")

        from lincy.workspace.migrations.m0008_post_reviewer_structured_actions import (
            M0008PostReviewerStructuredActions,
        )

        migration = M0008PostReviewerStructuredActions()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "new structured actions prompt"


class TestM0009ShutdownReviewerPrompt:
    """Tests for shutdown reviewer prompt migration."""

    def test_copies_shutdown_reviewer_prompt_from_template(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"
        src = templates_dir / "agents" / "shutdown_reviewer" / "prompts"
        dst = kernel_dir / "agents" / "shutdown_reviewer" / "prompts"
        src.mkdir(parents=True)
        (src / "system.md").write_text("shutdown reviewer prompt")

        from lincy.workspace.migrations.m0009_shutdown_reviewer_prompt import (
            M0009ShutdownReviewerPrompt,
        )

        migration = M0009ShutdownReviewerPrompt()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "shutdown reviewer prompt"


class TestM0010ReviewerParseRetryPrompts:
    """Tests for reviewer parse-retry prompt migration."""

    def test_copies_parse_retry_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        pre_src = templates_dir / "agents" / "pre_reviewer" / "prompts"
        post_src = templates_dir / "agents" / "post_reviewer" / "prompts"
        shutdown_src = templates_dir / "agents" / "shutdown_reviewer" / "prompts"
        pre_dst = kernel_dir / "agents" / "pre_reviewer" / "prompts"
        post_dst = kernel_dir / "agents" / "post_reviewer" / "prompts"
        shutdown_dst = kernel_dir / "agents" / "shutdown_reviewer" / "prompts"

        pre_src.mkdir(parents=True)
        post_src.mkdir(parents=True)
        shutdown_src.mkdir(parents=True)
        (pre_src / "parse-retry.md").write_text("pre parse retry prompt")
        (post_src / "parse-retry.md").write_text("post parse retry prompt")
        (shutdown_src / "parse-retry.md").write_text("shutdown parse retry prompt")

        from lincy.workspace.migrations.m0010_reviewer_parse_retry_prompts import (
            M0010ReviewerParseRetryPrompts,
        )

        migration = M0010ReviewerParseRetryPrompts()
        migration.upgrade(kernel_dir, templates_dir)

        assert (pre_dst / "parse-retry.md").read_text() == "pre parse retry prompt"
        assert (post_dst / "parse-retry.md").read_text() == "post parse retry prompt"
        assert (shutdown_dst / "parse-retry.md").read_text() == "shutdown parse retry prompt"


class TestM0011SystemPromptFormatting:
    """Tests for system prompt formatting migration."""

    def test_copies_system_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        src = templates_dir / "agents" / "brain" / "prompts"
        dst = kernel_dir / "agents" / "brain" / "prompts"

        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (dst / "system.md").write_text("old prompt")
        (src / "system.md").write_text("new prompt with formatting")

        from lincy.workspace.migrations.m0011_system_prompt_formatting import (
            M0011SystemPromptFormatting,
        )

        migration = M0011SystemPromptFormatting()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "new prompt with formatting"


class TestM0012TurnPersistencePromptTuning:
    """Tests for turn persistence prompt tuning migration."""

    def test_copies_brain_and_post_reviewer_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        brain_src = templates_dir / "agents" / "brain" / "prompts"
        post_src = templates_dir / "agents" / "post_reviewer" / "prompts"
        brain_dst = kernel_dir / "agents" / "brain" / "prompts"
        post_dst = kernel_dir / "agents" / "post_reviewer" / "prompts"

        brain_src.mkdir(parents=True)
        post_src.mkdir(parents=True)
        brain_dst.mkdir(parents=True)
        post_dst.mkdir(parents=True)

        (brain_dst / "system.md").write_text("old brain prompt")
        (post_dst / "system.md").write_text("old post prompt")
        (brain_src / "system.md").write_text("new brain prompt")
        (post_src / "system.md").write_text("new post prompt")

        from lincy.workspace.migrations.m0012_turn_persistence_prompt_tuning import (
            M0012TurnPersistencePromptTuning,
        )

        migration = M0012TurnPersistencePromptTuning()
        migration.upgrade(kernel_dir, templates_dir)

        assert (brain_dst / "system.md").read_text() == "new brain prompt"
        assert (post_dst / "system.md").read_text() == "new post prompt"


class TestM0013MemoryWriterPipeline:
    """Tests for memory writer pipeline prompt migration."""

    def test_copies_memory_writer_and_related_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        mappings = [
            ("agents/brain/prompts/system.md", "brain system"),
            ("agents/brain/prompts/shutdown.md", "brain shutdown"),
            ("agents/post_reviewer/prompts/system.md", "post reviewer"),
            ("agents/shutdown_reviewer/prompts/system.md", "shutdown reviewer"),
            ("agents/memory_writer/prompts/system.md", "memory writer system"),
            ("agents/memory_writer/prompts/parse-retry.md", "memory writer parse retry"),
        ]

        for relative_path, content in mappings:
            src = templates_dir / relative_path
            dst = kernel_dir / relative_path
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content)
            dst.write_text("old")

        from lincy.workspace.migrations.m0013_memory_writer_pipeline import (
            M0013MemoryWriterPipeline,
        )

        migration = M0013MemoryWriterPipeline()
        migration.upgrade(kernel_dir, templates_dir)

        for relative_path, content in mappings:
            assert (kernel_dir / relative_path).read_text() == content


class TestM0014RecentContextPriority:
    """Tests for recent-context priority prompt migration."""

    def test_copies_recent_context_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        mappings = [
            ("agents/brain/prompts/system.md", "brain recent-context prompt"),
            ("agents/pre_reviewer/prompts/system.md", "pre reviewer recent-context prompt"),
            ("agents/post_reviewer/prompts/system.md", "post reviewer recent-context prompt"),
        ]

        for relative_path, content in mappings:
            src = templates_dir / relative_path
            dst = kernel_dir / relative_path
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content)
            dst.write_text("old")

        from lincy.workspace.migrations.m0014_recent_context_priority import (
            M0014RecentContextPriority,
        )

        migration = M0014RecentContextPriority()
        migration.upgrade(kernel_dir, templates_dir)

        for relative_path, content in mappings:
            assert (kernel_dir / relative_path).read_text() == content


class TestM0066ProgressReviewer:
    """Tests for progress reviewer prompt migration."""

    def test_copies_progress_reviewer_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        mappings = [
            ("agents/progress_reviewer/prompts/system.md", "progress reviewer system"),
            ("agents/progress_reviewer/prompts/parse-retry.md", "progress reviewer parse retry"),
        ]

        for relative_path, content in mappings:
            src = templates_dir / relative_path
            dst = kernel_dir / relative_path
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content)
            dst.write_text("old")

        from lincy.workspace.migrations.m0066_progress_reviewer import (
            M0066ProgressReviewer,
        )

        migration = M0066ProgressReviewer()
        migration.upgrade(kernel_dir, templates_dir)

        for relative_path, content in mappings:
            assert (kernel_dir / relative_path).read_text() == content


class TestM0067CompletionReviewerPrompts:
    """Tests for completion-gate reviewer prompt migration."""

    def test_copies_completion_reviewer_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        mappings = [
            ("agents/post_reviewer/prompts/system.md", "post reviewer completion system"),
            ("agents/post_reviewer/prompts/parse-retry.md", "post reviewer completion parse"),
            ("agents/shutdown_reviewer/prompts/system.md", "shutdown reviewer completion system"),
            ("agents/shutdown_reviewer/prompts/parse-retry.md", "shutdown reviewer completion parse"),
            ("agents/progress_reviewer/prompts/system.md", "progress reviewer advisory system"),
            ("agents/progress_reviewer/prompts/parse-retry.md", "progress reviewer parse retry"),
        ]

        for relative_path, content in mappings:
            src = templates_dir / relative_path
            dst = kernel_dir / relative_path
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(content)
            dst.write_text("old")

        from lincy.workspace.migrations.m0067_completion_reviewer_prompts import (
            M0067CompletionReviewerPrompts,
        )

        migration = M0067CompletionReviewerPrompts()
        migration.upgrade(kernel_dir, templates_dir)

        for relative_path, content in mappings:
            assert (kernel_dir / relative_path).read_text() == content


class TestM0110BrainSendMessageSegments:
    """Tests for send_message segments prompt migration."""

    def test_copies_brain_prompt_from_template(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        src = templates_dir / "agents" / "brain" / "prompts"
        dst = kernel_dir / "agents" / "brain" / "prompts"

        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (dst / "system.md").write_text("legacy send_message body prompt")
        (src / "system.md").write_text("segments-only send_message prompt")

        from lincy.workspace.migrations.m0110_brain_send_message_segments import (
            M0110BrainSendMessageSegments,
        )

        migration = M0110BrainSendMessageSegments()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "segments-only send_message prompt"


class TestM0113DiscordMarkdownPrompt:
    """Tests for Discord Markdown prompt migration."""

    def test_copies_brain_prompt_from_template(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        src = templates_dir / "agents" / "brain" / "prompts"
        dst = kernel_dir / "agents" / "brain" / "prompts"

        src.mkdir(parents=True)
        dst.mkdir(parents=True)
        (dst / "system.md").write_text("legacy discord prompt")
        (src / "system.md").write_text("discord markdown prompt")

        from lincy.workspace.migrations.m0113_discord_markdown_prompt import (
            M0113DiscordMarkdownPrompt,
        )

        migration = M0113DiscordMarkdownPrompt()
        migration.upgrade(kernel_dir, templates_dir)

        assert (dst / "system.md").read_text() == "discord markdown prompt"


class TestM0114DiscordBuiltinSkill:
    """Tests for Discord builtin skill migration."""

    def test_copies_skill_files_and_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        skill_src = templates_dir / "builtin-skills"
        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"

        (skill_src / "discord-messaging").mkdir(parents=True)
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (skill_src / "index.md").write_text("builtin index")
        (skill_src / "discord-messaging" / "guide.md").write_text("discord skill guide")
        (prompt_src / "system.md").write_text("brain prompt routed to skill")
        (prompt_dst / "system.md").write_text("legacy discord prompt rules")

        from lincy.workspace.migrations.m0114_discord_builtin_skill import (
            M0114DiscordBuiltinSkill,
        )

        migration = M0114DiscordBuiltinSkill()
        migration.upgrade(kernel_dir, templates_dir)

        assert (kernel_dir / "builtin-skills" / "index.md").read_text() == "builtin index"
        assert (
            kernel_dir / "builtin-skills" / "discord-messaging" / "guide.md"
        ).read_text() == "discord skill guide"
        assert (prompt_dst / "system.md").read_text() == "brain prompt routed to skill"


class TestM0115DiscordPresentationStrategy:
    """Tests for Discord presentation strategy migration."""

    def test_copies_updated_skill_files_and_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        skill_src = templates_dir / "builtin-skills"
        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"

        (skill_src / "discord-messaging").mkdir(parents=True)
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (skill_src / "index.md").write_text("updated builtin index")
        (skill_src / "discord-messaging" / "guide.md").write_text("semantic presentation guide")
        (prompt_src / "system.md").write_text("brain prompt points to presentation strategy")
        (prompt_dst / "system.md").write_text("legacy discord routing")

        from lincy.workspace.migrations.m0115_discord_presentation_strategy import (
            M0115DiscordPresentationStrategy,
        )

        migration = M0115DiscordPresentationStrategy()
        migration.upgrade(kernel_dir, templates_dir)

        assert (kernel_dir / "builtin-skills" / "index.md").read_text() == "updated builtin index"
        assert (
            kernel_dir / "builtin-skills" / "discord-messaging" / "guide.md"
        ).read_text() == "semantic presentation guide"
        assert (prompt_dst / "system.md").read_text() == "brain prompt points to presentation strategy"


class TestM0116DiscordNaturalLists:
    """Tests for Discord natural-list phrasing migration."""

    def test_copies_updated_builtin_skill_files(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        skill_src = templates_dir / "builtin-skills"
        (skill_src / "discord-messaging").mkdir(parents=True)

        (skill_src / "index.md").write_text("natural list index")
        (skill_src / "discord-messaging" / "guide.md").write_text("natural list guide")

        from lincy.workspace.migrations.m0116_discord_natural_lists import (
            M0116DiscordNaturalLists,
        )

        migration = M0116DiscordNaturalLists()
        migration.upgrade(kernel_dir, templates_dir)

        assert (kernel_dir / "builtin-skills" / "index.md").read_text() == "natural list index"
        assert (
            kernel_dir / "builtin-skills" / "discord-messaging" / "guide.md"
        ).read_text() == "natural list guide"


class TestM0117DiscordMessageEconomy:
    """Tests for Discord same-turn message-economy migration."""

    def test_copies_updated_builtin_skill_and_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        skill_src = templates_dir / "builtin-skills" / "discord-messaging"
        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"

        skill_src.mkdir(parents=True)
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (skill_src / "guide.md").write_text("message economy guide")
        (prompt_src / "system.md").write_text("message economy brain prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0117_discord_message_economy import (
            M0117DiscordMessageEconomy,
        )

        migration = M0117DiscordMessageEconomy()
        migration.upgrade(kernel_dir, templates_dir)

        assert (
            kernel_dir / "builtin-skills" / "discord-messaging" / "guide.md"
        ).read_text() == "message economy guide"
        assert (prompt_dst / "system.md").read_text() == "message economy brain prompt"


class TestM0118SkillPrerequisiteMetadata:
    """Tests for skill prerequisite metadata migration."""

    def test_copies_discord_skill_metadata(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        skill_src = templates_dir / "builtin-skills" / "discord-messaging"
        skill_src.mkdir(parents=True)
        (skill_src / "meta.yaml").write_text("id: discord-messaging\n")

        from lincy.workspace.migrations.m0118_skill_prerequisite_metadata import (
            M0118SkillPrerequisiteMetadata,
        )

        migration = M0118SkillPrerequisiteMetadata()
        migration.upgrade(kernel_dir, templates_dir)

        assert (
            kernel_dir / "builtin-skills" / "discord-messaging" / "meta.yaml"
        ).read_text() == "id: discord-messaging\n"


class TestM0120ShellNonInteractive:
    """Tests for execute_shell non-interactive prompt migration."""

    def test_copies_updated_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (prompt_src / "system.md").write_text("non-interactive execute_shell prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0120_shell_noninteractive import (
            M0120ShellNonInteractive,
        )

        migration = M0120ShellNonInteractive()
        migration.upgrade(kernel_dir, templates_dir)

        assert (prompt_dst / "system.md").read_text() == "non-interactive execute_shell prompt"


class TestM0121ShellTask:
    """Tests for shell_task prompt migration."""

    def test_copies_updated_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (prompt_src / "system.md").write_text("shell_task prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0121_shell_task import (
            M0121ShellTask,
        )

        migration = M0121ShellTask()
        migration.upgrade(kernel_dir, templates_dir)

        assert (prompt_dst / "system.md").read_text() == "shell_task prompt"


class TestM0122WebSearch:
    """Tests for web_search prompt migration."""

    def test_copies_updated_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (prompt_src / "system.md").write_text("web_search prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0122_web_search import (
            M0122WebSearch,
        )

        migration = M0122WebSearch()
        migration.upgrade(kernel_dir, templates_dir)

        assert (prompt_dst / "system.md").read_text() == "web_search prompt"


class TestM0123ShellTaskHandoff:
    """Tests for shell_task handoff prompt migration."""

    def test_copies_updated_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (prompt_src / "system.md").write_text("shell_task handoff prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0123_shell_task_handoff import (
            M0123ShellTaskHandoff,
        )

        migration = M0123ShellTaskHandoff()
        migration.upgrade(kernel_dir, templates_dir)

        assert (prompt_dst / "system.md").read_text() == "shell_task handoff prompt"


class TestM0127WebFetch:
    """Tests for web_fetch prompt migration."""

    def test_copies_updated_brain_prompt(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        prompt_src = templates_dir / "agents" / "brain" / "prompts"
        prompt_dst = kernel_dir / "agents" / "brain" / "prompts"
        prompt_src.mkdir(parents=True)
        prompt_dst.mkdir(parents=True)

        (prompt_src / "system.md").write_text("web_fetch prompt")
        (prompt_dst / "system.md").write_text("legacy brain prompt")

        from lincy.workspace.migrations.m0127_web_fetch import (
            M0127WebFetch,
        )

        migration = M0127WebFetch()
        migration.upgrade(kernel_dir, templates_dir)

        assert (prompt_dst / "system.md").read_text() == "web_fetch prompt"


class TestM0128GuiLoadingScrollPrompts:
    """Tests for GUI loading/scroll prompt migration."""

    def test_copies_updated_gui_prompts(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        files = [
            "agents/gui_manager/prompts/system.md",
            "agents/gui_worker/prompts/system.md",
            "agents/gui_worker/prompts/layout.md",
        ]

        for rel in files:
            src = templates_dir / rel
            dst = kernel_dir / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"new::{rel}")
            dst.write_text(f"old::{rel}")

        from lincy.workspace.migrations.m0128_gui_loading_scroll_prompts import (
            M0128GuiLoadingScrollPrompts,
        )

        migration = M0128GuiLoadingScrollPrompts()
        migration.upgrade(kernel_dir, templates_dir)

        for rel in files:
            assert (kernel_dir / rel).read_text() == f"new::{rel}"


class TestM0134DiscordAttachmentContext:
    """Tests for Discord attachment guidance migration."""

    def test_copies_updated_brain_and_discord_skill_files(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        files = [
            "agents/brain/prompts/system.md",
            "builtin-skills/discord-messaging/guide.md",
        ]

        for rel in files:
            src = templates_dir / rel
            dst = kernel_dir / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"new::{rel}")
            dst.write_text(f"old::{rel}")

        from lincy.workspace.migrations.m0134_discord_attachment_context import (
            M0134DiscordAttachmentContext,
        )

        migration = M0134DiscordAttachmentContext()
        migration.upgrade(kernel_dir, templates_dir)

        for rel in files:
            assert (kernel_dir / rel).read_text() == f"new::{rel}"


class TestM0137SkillInstallerRepoAtSkill:
    """Tests for skill-installer command format migration."""

    def test_copies_updated_skill_installer_guide(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        rel = "builtin-skills/skill-installer/SKILL.md"
        src = templates_dir / rel
        dst = kernel_dir / rel
        src.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("new::repo-at-skill")
        dst.write_text("old::repo-and-skill-flag")

        from lincy.workspace.migrations.m0137_skill_installer_repo_at_skill import (
            M0137SkillInstallerRepoAtSkill,
        )

        migration = M0137SkillInstallerRepoAtSkill()
        migration.upgrade(kernel_dir, templates_dir)

        assert dst.read_text() == "new::repo-at-skill"


class TestM0163BrainShellDelegation:
    """Tests for brain shell delegation migration."""

    def test_copies_brain_worker_prompts_and_memory_skill(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        files = [
            "agents/brain/prompts/system.md",
            "agents/worker/prompts/system.md",
            "builtin-skills/memory-maintenance/SKILL.md",
        ]

        for rel in files:
            src = templates_dir / rel
            dst = kernel_dir / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"new::{rel}")
            dst.write_text(f"old::{rel}")

        from lincy.workspace.migrations.m0163_brain_shell_delegation import (
            M0163BrainShellDelegation,
        )

        migration = M0163BrainShellDelegation()
        migration.upgrade(kernel_dir, templates_dir)

        for rel in files:
            assert (kernel_dir / rel).read_text() == f"new::{rel}"


class TestM0164MemoryMaintenanceWorkerNative:
    """Tests for worker-native memory maintenance skill migration."""

    def test_copies_skill_and_rules(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"

        files = [
            "builtin-skills/memory-maintenance/SKILL.md",
            "builtin-skills/memory-maintenance/references/rules.md",
        ]

        for rel in files:
            src = templates_dir / rel
            dst = kernel_dir / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"new::{rel}")
            dst.write_text(f"old::{rel}")

        from lincy.workspace.migrations.m0164_memory_maintenance_worker_native import (
            M0164MemoryMaintenanceWorkerNative,
        )

        migration = M0164MemoryMaintenanceWorkerNative()
        migration.upgrade(kernel_dir, templates_dir)

        for rel in files:
            assert (kernel_dir / rel).read_text() == f"new::{rel}"

    def test_missing_template_is_noop(self, tmp_path: Path):
        kernel_dir = tmp_path / "kernel"
        templates_dir = tmp_path / "templates"
        kernel_dir.mkdir()
        templates_dir.mkdir()

        from lincy.workspace.migrations.m0164_memory_maintenance_worker_native import (
            M0164MemoryMaintenanceWorkerNative,
        )

        migration = M0164MemoryMaintenanceWorkerNative()
        migration.upgrade(kernel_dir, templates_dir)

        assert not (kernel_dir / "builtin-skills").exists()
