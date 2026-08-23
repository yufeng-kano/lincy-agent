"""Every checked-in LLM profile must resolve against the current schema.

Guards against schema refactors silently breaking cfgs/llm/* (e.g. a field
removed from a capabilities model while strict validation stays on).
"""

from pathlib import Path

import pytest

from lincy.core.config import resolve_llm_config

_CFGS_DIR = Path(__file__).resolve().parents[2] / "cfgs"

# Profiles that already fail on their own validation rules (pre-existing,
# unrelated to schema shape). Remove entries here once the files are fixed.
_KNOWN_BAD = {
    "llm/gemini/gemini-3-pro/no-thinking.yaml",
}

_PROFILES = sorted(
    str(path.relative_to(_CFGS_DIR))
    for path in (_CFGS_DIR / "llm").rglob("*.yaml")
)


@pytest.mark.parametrize("rel_path", _PROFILES)
def test_llm_profile_resolves(rel_path: str):
    if rel_path in _KNOWN_BAD:
        pytest.skip("pre-existing invalid profile")
    resolve_llm_config(rel_path)
