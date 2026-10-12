"""Workspace initialization utilities."""

from importlib import resources
import json
from pathlib import Path
import re
import shutil

from ..skills import PERSONAL_SKILLS_DIR, rebuild_personal_skills_index
from .backup import WorkspaceBackup
from .manager import WorkspaceManager
from .migrator import MigrationResult, Migrator


_PROMPT_DUPLICATE_RE = re.compile(r"^(?P<stem>.+) (?P<index>\d+)(?P<suffix>\.[^.]+)$")


class WorkspaceInitializer:
    """Handles workspace creation and upgrades."""

    def __init__(self, manager: WorkspaceManager):
        self.manager = manager
        self._migrator: Migrator | None = None

    @property
    def migrator(self) -> Migrator:
        """Lazy-loaded migrator instance."""
        if self._migrator is None:
            templates_dir = self._get_templates_dir()
            self._migrator = Migrator(
                self.manager.kernel_dir,
                templates_dir / "kernel",
            )
        return self._migrator

    def create_structure(self) -> None:
        """Copy templates to agent_os_dir (kernel + memory + personal skills).

        Creates the complete directory structure from package templates.
        Does nothing if workspace already exists.
        """
        if self.manager.is_initialized():
            return

        # Get templates directory from package resources
        templates_dir = self._get_templates_dir()

        # Create working directory
        self.manager.agent_os_dir.mkdir(parents=True, exist_ok=True)

        # Copy kernel/ (always overwrite)
        kernel_src = templates_dir / "kernel"
        if kernel_src.exists():
            shutil.copytree(kernel_src, self.manager.kernel_dir)

        # Copy memory/ (only if not exists)
        if not self.manager.memory_dir.exists():
            memory_src = templates_dir / "memory"
            if memory_src.exists():
                shutil.copytree(memory_src, self.manager.memory_dir)

        # Copy personal-skills/ (only if not exists)
        if not self.manager.personal_skills_dir.exists():
            personal_src = templates_dir / PERSONAL_SKILLS_DIR
            if personal_src.exists():
                shutil.copytree(personal_src, self.manager.personal_skills_dir)

        # New workspaces keep personal skills outside memory/.
        legacy_skills_dir = self.manager.memory_dir / "agent" / "skills"
        if legacy_skills_dir.exists():
            shutil.rmtree(legacy_skills_dir)

        rebuild_personal_skills_index(self.manager.agent_os_dir)

        # Runtime-only directories that should exist even before first use.
        state_dir = self.manager.agent_os_dir / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "artifacts" / "files").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "artifacts" / "creations").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "cache" / "apple_notes").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "cache" / "vision").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "cache" / "audio").mkdir(parents=True, exist_ok=True)
        self._ensure_runtime_state_files(state_dir)

        self._prune_managed_prompt_duplicates(kernel_templates_dir=templates_dir / "kernel")

    def needs_upgrade(self) -> bool:
        """Check if kernel upgrade is needed."""
        if not self.manager.is_initialized():
            return True
        return self.migrator.needs_migration()

    def upgrade_kernel(self) -> "MigrationResult":
        """Run pending migrations.

        Backs up upgrade-managed dirs (kernel/memory/personal-skills)
        before applying any migration.

        Returns:
            MigrationResult with applied versions and summaries.
        """

        backup = WorkspaceBackup(self.manager.agent_os_dir)
        current_version = self.manager.get_kernel_version()
        backup.create_backup(current_version)

        result = self.migrator.run_migrations()
        rebuild_personal_skills_index(self.manager.agent_os_dir)
        self._ensure_runtime_state_files(self.manager.agent_os_dir / "state")
        (self.manager.agent_os_dir / "cache" / "apple_notes").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "cache" / "vision").mkdir(parents=True, exist_ok=True)
        (self.manager.agent_os_dir / "cache" / "audio").mkdir(parents=True, exist_ok=True)
        self._prune_managed_prompt_duplicates()
        return result

    def _get_templates_dir(self) -> Path:
        """Get the templates directory from package resources."""
        # Use importlib.resources for proper package resource access
        pkg_files = resources.files("lincy.workspace")
        # For development, templates are in the same directory
        # Return the path directly since we're in a source layout
        return Path(str(pkg_files)) / "templates"

    def _prune_managed_prompt_duplicates(self, kernel_templates_dir: Path | None = None) -> None:
        """Delete Finder/iCloud duplicate prompt files after managed kernel writes."""
        templates_kernel = kernel_templates_dir or self._get_templates_dir() / "kernel"
        agents_templates_dir = templates_kernel / "agents"
        if not agents_templates_dir.exists():
            return

        for prompt_template in agents_templates_dir.glob("*/prompts/*"):
            if not prompt_template.is_file():
                continue
            runtime_dir = self.manager.kernel_dir / prompt_template.relative_to(templates_kernel).parent
            if not runtime_dir.exists():
                continue
            self._prune_prompt_dir_duplicates(runtime_dir, prompt_template.name)

    @staticmethod
    def _prune_prompt_dir_duplicates(prompt_dir: Path, canonical_name: str) -> None:
        """Keep canonical managed prompt files and remove numbered conflict copies."""
        canonical_path = prompt_dir / canonical_name
        if not canonical_path.exists():
            return

        stem = canonical_path.stem
        suffix = canonical_path.suffix
        for candidate in prompt_dir.iterdir():
            if not candidate.is_file():
                continue
            if candidate.name == ".DS_Store":
                candidate.unlink(missing_ok=True)
                continue
            if candidate.name == canonical_name:
                continue
            match = _PROMPT_DUPLICATE_RE.match(candidate.name)
            if not match:
                continue
            if match.group("stem") != stem or match.group("suffix") != suffix:
                continue
            candidate.unlink(missing_ok=True)

    @staticmethod
    def _ensure_runtime_state_files(state_dir: Path) -> None:
        """Seed empty runtime state files so every workspace starts consistent."""
        state_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in (
            ("notes.json", {"notes": {}}),
            ("tasks.json", {"tasks": [], "next_id": 1}),
        ):
            path = state_dir / filename
            if path.exists():
                continue
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(path)
