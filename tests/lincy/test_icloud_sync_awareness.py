from types import SimpleNamespace

from lincy.brain_prompt_policy import BrainPromptPolicy
from lincy.icloud_sync_awareness import (
    SYSTEM_PROMPT_FRAGMENT_PATH,
    SYSTEM_PROMPT_PLACEHOLDER,
)


def _config(enabled: bool):
    return SimpleNamespace(
        features=SimpleNamespace(
            icloud_sync_awareness=SimpleNamespace(enabled=enabled),
        )
    )


def test_system_prompt_placeholder_resolves_when_enabled(tmp_path):
    fragment_path = tmp_path / SYSTEM_PROMPT_FRAGMENT_PATH
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text("icloud fragment", encoding="utf-8")

    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(True))
    prompt = f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n\nafter\n"

    resolved = policy.resolve(prompt)

    assert SYSTEM_PROMPT_PLACEHOLDER not in resolved
    assert "icloud fragment" in resolved
    assert "before" in resolved
    assert "after" in resolved


def test_system_prompt_placeholder_drops_when_disabled(tmp_path):
    fragment_path = tmp_path / SYSTEM_PROMPT_FRAGMENT_PATH
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text("icloud fragment", encoding="utf-8")

    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(False))
    prompt = f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n\nafter\n"

    resolved = policy.resolve(prompt)

    assert SYSTEM_PROMPT_PLACEHOLDER not in resolved
    assert "icloud fragment" not in resolved
    assert "before" in resolved
    assert "after" in resolved


def test_missing_feature_config_defaults_to_disabled(tmp_path):
    policy = BrainPromptPolicy(
        kernel_dir=tmp_path,
        config=SimpleNamespace(features=SimpleNamespace()),
    )

    resolved = policy.resolve(f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n")

    assert SYSTEM_PROMPT_PLACEHOLDER not in resolved
    assert resolved == "before\n"
