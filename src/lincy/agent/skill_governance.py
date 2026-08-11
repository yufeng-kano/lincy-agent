"""Runtime skill prerequisite governance for tool execution."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
from typing import TYPE_CHECKING
import uuid

from pydantic import ValidationError
import yaml

from ..core.schema import GovernanceRule, SkillGovernanceConfig
from ..llm.schema import Message, ToolCall, make_tool_result_message
from ..skills import (
    BUILTIN_SKILLS_DIR,
    PERSONAL_SKILLS_DIR,
    SKILL_ENTRY_FILE,
    SKILL_METADATA_FILE,
    SkillMetadata,
    parse_skill_frontmatter,
    rebuild_personal_skills_index,
)

if TYPE_CHECKING:
    from ..context.conversation import Conversation

logger = logging.getLogger(__name__)

SKILL_PREREQUISITE_TOOL_NAME = "_load_skill_prerequisite"
# ---------------------------------------------------------------------------
# Runtime data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillRequirement:
    """Resolved prerequisite for a governed tool call."""

    skill_name: str
    guide_path: Path
    guide_rel_path: str


@dataclass(frozen=True)
class InjectedSkillGuide:
    """Synthetic assistant/tool pair for one injected skill guide."""

    call: ToolCall
    content: str
    skill_name: str


@dataclass(frozen=True)
class SkillCatalogEntry:
    """Minimal skill metadata exposed to the proactive selector."""

    name: str
    description: str


@dataclass(frozen=True)
class _RegisteredSkill:
    """Runtime registration for one skill package."""

    metadata: SkillMetadata
    guide_path: Path
    guide_rel_path: str


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SkillGovernanceRegistry:
    """Registry of skills that govern tool usage."""

    def __init__(
        self,
        *,
        agent_os_dir: Path,
        skills: dict[str, _RegisteredSkill],
        rules: list[GovernanceRule] | None = None,
        governance_config: SkillGovernanceConfig | None = None,
    ):
        self._agent_os_dir = agent_os_dir
        self._skills = skills
        self._rules = rules or []
        self._governance_config = governance_config or SkillGovernanceConfig()
        self._guide_index = {
            skill.guide_path.resolve(): skill.metadata.name
            for skill in skills.values()
        }
        self._skill_mtimes = self._snapshot_skill_mtimes()

    # -- hot reload -------------------------------------------------------

    def _snapshot_skill_mtimes(self) -> dict[str, float]:
        """Collect mtime of every SKILL.md across all roots."""
        mtimes: dict[str, float] = {}
        for skill_md in _iter_skill_entry_files(self._get_root_paths()):
            try:
                mtimes[str(skill_md)] = skill_md.stat().st_mtime
            except OSError:
                pass
        return mtimes

    def _get_root_paths(self) -> list[tuple[Path, bool]]:
        """Return (root_path, is_external) for all configured roots."""
        roots: list[tuple[Path, bool]] = [
            (self._agent_os_dir / BUILTIN_SKILLS_DIR, False),
            (self._agent_os_dir / PERSONAL_SKILLS_DIR, False),
        ]
        if self._governance_config.external_skills_dir:
            ext = Path(self._governance_config.external_skills_dir).expanduser().resolve()
            roots.append((ext, True))
        return roots

    def needs_rescan(self) -> bool:
        """Check if any skill root has changed since last load."""
        return self._snapshot_skill_mtimes() != self._skill_mtimes

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(
        cls,
        agent_os_dir: Path,
        *,
        governance_config: SkillGovernanceConfig | None = None,
    ) -> "SkillGovernanceRegistry":
        """Load skills from three roots with priority-based dedup."""
        config = governance_config or SkillGovernanceConfig()
        rebuild_personal_skills_index(agent_os_dir)
        skills: dict[str, _RegisteredSkill] = {}

        # Priority order: builtin > personal > external
        roots: list[tuple[str, Path, bool]] = [
            ("builtin", agent_os_dir / BUILTIN_SKILLS_DIR, False),
            ("personal", agent_os_dir / PERSONAL_SKILLS_DIR, False),
        ]
        if config.external_skills_dir:
            ext = Path(config.external_skills_dir).expanduser().resolve()
            roots.append(("external", ext, True))

        for label, root, is_external in roots:
            if not root.exists():
                continue
            _scan_skill_root(agent_os_dir, root, skills, is_external=is_external)

        rules = list(config.rules)
        # Startup validation: warn about governance rules that reference
        # skills not yet loaded (they may be installed later).
        for rule in rules:
            if rule.skill not in skills:
                logger.warning(
                    "Governance rule references unknown skill '%s'",
                    rule.skill,
                )

        return cls(
            agent_os_dir=agent_os_dir,
            skills=skills,
            rules=rules,
            governance_config=config,
        )

    # -- prerequisite lookup ----------------------------------------------

    def find_missing_requirements(
        self,
        tool_calls: list[ToolCall],
        *,
        loaded_skill_names: set[str],
    ) -> list[SkillRequirement]:
        """Return unique missing prerequisites for the given tool batch."""
        ordered: list[SkillRequirement] = []
        seen: set[str] = set()
        for tool_call in tool_calls:
            for requirement in self.requirements_for_tool_call(tool_call):
                if requirement.skill_name in loaded_skill_names or requirement.skill_name in seen:
                    continue
                seen.add(requirement.skill_name)
                ordered.append(requirement)
        return ordered

    def requirements_for_tool_call(self, tool_call: ToolCall) -> list[SkillRequirement]:
        """Return all enforced prerequisites for one tool call."""
        matches: list[SkillRequirement] = []
        for rule in self._rules:
            if rule.tool != tool_call.name:
                continue
            if rule.enforcement != "require_context":
                continue
            if not _rule_matches_arguments(rule.when, tool_call.arguments):
                continue
            skill = self._skills.get(rule.skill)
            if skill is None:
                continue
            matches.append(
                SkillRequirement(
                    skill_name=skill.metadata.name,
                    guide_path=skill.guide_path,
                    guide_rel_path=skill.guide_rel_path,
                )
            )
        return matches

    # -- guide path tracking ----------------------------------------------

    def note_loaded_guide(self, *, path: str) -> str | None:
        """Return skill name when a read_file path matches a skill guide."""
        target = Path(path)
        if not target.is_absolute():
            target = self._agent_os_dir / target
        try:
            resolved = target.resolve()
        except Exception:
            resolved = target.resolve(strict=False)
        return self._guide_index.get(resolved)

    # -- conversation scanning --------------------------------------------

    def loaded_skill_names_from_conversation(
        self,
        conversation: "Conversation",
    ) -> set[str]:
        """Return skill names whose guides are still present in conversation."""
        loaded: set[str] = set()
        pending_injected: dict[str, str] = {}
        pending_reads: dict[str, str] = {}

        for entry in conversation.get_messages():
            if entry.role == "assistant" and entry.tool_calls:
                for tool_call in entry.tool_calls:
                    if tool_call.name == SKILL_PREREQUISITE_TOOL_NAME:
                        # Backward compat: check both skill_name and skill_id
                        skill_name = (
                            tool_call.arguments.get("skill_name")
                            or tool_call.arguments.get("skill_id")
                        )
                        if isinstance(skill_name, str):
                            pending_injected[tool_call.id] = skill_name
                        continue
                    if tool_call.name != "read_file":
                        continue
                    path = tool_call.arguments.get("path")
                    if not isinstance(path, str):
                        continue
                    skill_name = self.note_loaded_guide(path=path)
                    if skill_name is not None:
                        pending_reads[tool_call.id] = skill_name
                continue

            if entry.role != "tool" or not isinstance(entry.tool_call_id, str):
                continue

            injected_name = pending_injected.get(entry.tool_call_id)
            if injected_name is not None and entry.name == SKILL_PREREQUISITE_TOOL_NAME:
                loaded.add(injected_name)

            read_name = pending_reads.get(entry.tool_call_id)
            if read_name is not None and entry.name == "read_file":
                loaded.add(read_name)
        return loaded

    def list_skill_catalog(
        self,
        *,
        exclude_skill_names: set[str] | None = None,
    ) -> list[SkillCatalogEntry]:
        """Return stable metadata for proactive skill selection."""
        excluded = exclude_skill_names or set()
        catalog: list[SkillCatalogEntry] = []
        for name in sorted(self._skills):
            if name in excluded:
                continue
            skill = self._skills[name]
            catalog.append(
                SkillCatalogEntry(
                    name=skill.metadata.name,
                    description=skill.metadata.description,
                )
            )
        return catalog

    def requirements_for_skill_names(
        self,
        skill_names: list[str],
        *,
        loaded_skill_names: set[str] | None = None,
    ) -> list[SkillRequirement]:
        """Resolve exact skill names into injectable guide requirements."""
        loaded = loaded_skill_names or set()
        requirements: list[SkillRequirement] = []
        seen: set[str] = set()
        for skill_name in skill_names:
            if skill_name in loaded or skill_name in seen:
                continue
            skill = self._skills.get(skill_name)
            if skill is None:
                continue
            seen.add(skill_name)
            requirements.append(
                SkillRequirement(
                    skill_name=skill.metadata.name,
                    guide_path=skill.guide_path,
                    guide_rel_path=skill.guide_rel_path,
                )
            )
        return requirements

    # -- guide injection --------------------------------------------------

    def build_injected_guides(
        self,
        requirements: list[SkillRequirement],
    ) -> list[InjectedSkillGuide]:
        """Build synthetic assistant/tool pairs for required guides."""
        injected: list[InjectedSkillGuide] = []
        for requirement in requirements:
            content = _load_guide_content(requirement)
            if content is None:
                continue
            call = ToolCall(
                id=f"skill_{uuid.uuid4().hex[:8]}",
                name=SKILL_PREREQUISITE_TOOL_NAME,
                arguments={
                    "skill_name": requirement.skill_name,
                    "path": requirement.guide_rel_path,
                },
            )
            injected.append(
                InjectedSkillGuide(
                    call=call,
                    content=content,
                    skill_name=requirement.skill_name,
                )
            )
        return injected


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def build_skill_prerequisite_messages(
    injected: InjectedSkillGuide,
) -> tuple[Message, Message]:
    """Build a synthetic assistant/tool pair for one loaded skill guide."""
    call_msg = Message(
        role="assistant",
        content=None,
        tool_calls=[injected.call],
    )
    result_msg = make_tool_result_message(
        tool_call_id=injected.call.id,
        name=injected.call.name,
        content=injected.content,
    )
    return call_msg, result_msg


def build_skill_deferral_text(
    *,
    missing_skill_names: list[str],
) -> str:
    """Build tool deferral text when prerequisites were injected first."""
    joined = ", ".join(missing_skill_names)
    return (
        "Error: Deferred this tool round until required skill guide(s) "
        f"were loaded into context: {joined}. Review the loaded guide and "
        "retry any tool calls that are still appropriate."
    )


# ---------------------------------------------------------------------------
# Internal: scanning and loading
# ---------------------------------------------------------------------------

def _iter_skill_entry_files(roots: list[tuple[Path, bool]]):
    """Yield skill entry files, including one package level for external roots."""
    for root, is_external in roots:
        if not root.exists():
            continue
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            skill_md = child / SKILL_ENTRY_FILE
            meta_yaml = child / SKILL_METADATA_FILE
            if skill_md.exists():
                yield skill_md
            elif meta_yaml.exists():
                yield meta_yaml
            if is_external:
                for grandchild in sorted(child.iterdir()):
                    if not grandchild.is_dir():
                        continue
                    nested_md = grandchild / SKILL_ENTRY_FILE
                    nested_meta = grandchild / SKILL_METADATA_FILE
                    if nested_md.exists():
                        yield nested_md
                    elif nested_meta.exists():
                        yield nested_meta


def _scan_skill_root(
    agent_os_dir: Path,
    root: Path,
    skills: dict[str, _RegisteredSkill],
    *,
    is_external: bool = False,
) -> None:
    """Scan one root using the same entry discovery as hot reload."""
    for skill_md in _iter_skill_entry_files([(root, is_external)]):
        registered = _load_skill_from_dir(
            agent_os_dir,
            skill_md.parent,
            is_external=is_external,
        )
        if registered is not None:
            _register_skill(skills, registered)


def _register_skill(
    skills: dict[str, _RegisteredSkill],
    registered: _RegisteredSkill,
) -> None:
    """Register a skill, skipping if a higher-priority one already exists."""
    name = registered.metadata.name
    if name in skills:
        logger.warning(
            "Skill '%s' at %s shadowed by higher-priority %s",
            name,
            registered.guide_rel_path,
            skills[name].guide_rel_path,
        )
        return
    skills[name] = registered


def _load_skill_from_dir(
    agent_os_dir: Path,
    skill_dir: Path,
    *,
    is_external: bool = False,
) -> _RegisteredSkill | None:
    """Load a skill from a directory containing SKILL.md or meta.yaml."""
    skill_md = skill_dir / SKILL_ENTRY_FILE
    meta_yaml = skill_dir / SKILL_METADATA_FILE

    if skill_md.exists():
        return _load_from_skill_md(agent_os_dir, skill_md, skill_dir, is_external=is_external)

    # Fallback: meta.yaml only when SKILL.md is absent
    if meta_yaml.exists():
        return _load_from_meta_yaml_fallback(agent_os_dir, meta_yaml, is_external=is_external)

    return None


def _load_from_skill_md(
    agent_os_dir: Path,
    skill_md: Path,
    skill_dir: Path,
    *,
    is_external: bool = False,
) -> _RegisteredSkill | None:
    """Load skill metadata from SKILL.md frontmatter."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError as error:
        logger.warning("Failed to read %s: %s", skill_md, error)
        return None

    raw = parse_skill_frontmatter(text)
    if not raw:
        logger.warning("Skipping skill at %s: no valid frontmatter", skill_md)
        return None

    # Default name from directory if not in frontmatter
    if "name" not in raw:
        raw["name"] = skill_dir.name

    try:
        metadata = SkillMetadata.model_validate(raw)
    except ValidationError as error:
        logger.warning("Skipping skill at %s: %s", skill_md, error)
        return None

    guide_path = skill_md.resolve(strict=False)
    if is_external:
        guide_rel_path = str(skill_md)
    else:
        try:
            guide_rel_path = str(guide_path.relative_to(agent_os_dir))
        except ValueError:
            logger.warning(
                "Skipping skill '%s': guide path %s is outside agent_os_dir %s",
                metadata.name, guide_path, agent_os_dir,
            )
            return None

    return _RegisteredSkill(
        metadata=metadata,
        guide_path=guide_path,
        guide_rel_path=guide_rel_path,
    )


def _load_from_meta_yaml_fallback(
    agent_os_dir: Path,
    meta_path: Path,
    *,
    is_external: bool = False,
) -> _RegisteredSkill | None:
    """Load skill metadata from legacy meta.yaml (deprecated)."""
    logger.warning(
        "Skill at %s uses deprecated meta.yaml; migrate to SKILL.md frontmatter",
        meta_path.parent,
    )
    try:
        raw = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        logger.warning("Skipping invalid skill metadata %s: %s", meta_path, error)
        return None

    if not isinstance(raw, dict):
        logger.warning("Skipping skill at %s: meta.yaml is not a mapping", meta_path)
        return None

    # Map legacy fields: id -> name, build synthetic description
    skill_id = raw.get("id")
    guide_rel = raw.get("guide", "guide.md")
    if not isinstance(guide_rel, str):
        guide_rel = "guide.md"
    if not isinstance(skill_id, str):
        logger.warning("Skipping skill at %s: missing or invalid 'id'", meta_path)
        return None

    guide_path = (meta_path.parent / guide_rel).resolve(strict=False)
    if not guide_path.exists():
        logger.warning(
            "Skipping skill '%s': guide file missing at %s",
            skill_id, guide_path,
        )
        return None

    if is_external:
        rel_path = str(guide_path)
    else:
        try:
            rel_path = str(guide_path.relative_to(agent_os_dir))
        except ValueError:
            logger.warning(
                "Skipping skill '%s': guide path %s is outside agent_os_dir %s",
                skill_id, guide_path, agent_os_dir,
            )
            return None

    metadata = SkillMetadata(
        name=skill_id,
        description=f"(legacy) {skill_id}",
    )
    return _RegisteredSkill(
        metadata=metadata,
        guide_path=guide_path,
        guide_rel_path=rel_path,
    )


def _rule_matches_arguments(
    when: dict[str, object],
    arguments: dict[str, object],
) -> bool:
    for key, expected in when.items():
        if arguments.get(key) != expected:
            return False
    return True


def _load_guide_content(requirement: SkillRequirement) -> str | None:
    try:
        content = requirement.guide_path.read_text(encoding="utf-8").rstrip()
    except OSError as error:
        logger.warning(
            "Failed to load required skill '%s' from %s: %s",
            requirement.skill_name,
            requirement.guide_path,
            error,
        )
        return None

    return (
        "[Required Skill Guide Loaded]\n"
        f"skill_name: {requirement.skill_name}\n"
        f"path: {requirement.guide_rel_path}\n\n"
        f'<file path="{requirement.guide_rel_path}">\n'
        f"{content}\n"
        "</file>"
    )
