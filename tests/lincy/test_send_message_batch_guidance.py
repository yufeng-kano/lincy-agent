from types import SimpleNamespace

from lincy.brain_prompt_policy import BrainPromptPolicy
from lincy.context.builder import ContextBuilder
from lincy.context.conversation import Conversation
from lincy.send_message_batch_guidance import (
    SYSTEM_PROMPT_FRAGMENT_PATH,
    SYSTEM_PROMPT_PLACEHOLDER,
    build_prompt_fragment_spec,
)
from lincy.workspace.prompt_resolver import KernelPromptResolver


def _config(enabled: bool):
    return SimpleNamespace(
        features=SimpleNamespace(
            send_message_batch_guidance=SimpleNamespace(enabled=enabled),
        )
    )


def test_system_prompt_placeholder_resolves_from_kernel_fragment(tmp_path):
    fragment_path = tmp_path / SYSTEM_PROMPT_FRAGMENT_PATH
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text("fragment text", encoding="utf-8")

    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(True))
    prompt = f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n\nafter\n"
    resolved = policy.resolve(prompt)

    assert SYSTEM_PROMPT_PLACEHOLDER not in resolved
    assert "fragment text" in resolved
    assert "before" in resolved
    assert "after" in resolved


def test_system_prompt_placeholder_drops_when_disabled(tmp_path):
    fragment_path = tmp_path / SYSTEM_PROMPT_FRAGMENT_PATH
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text("fragment text", encoding="utf-8")

    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(False))
    prompt = f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n\nafter\n"
    resolved = policy.resolve(prompt)

    assert SYSTEM_PROMPT_PLACEHOLDER not in resolved
    assert "fragment text" not in resolved
    assert "before" in resolved
    assert "after" in resolved


def test_legacy_system_prompt_block_is_removed_when_disabled(tmp_path):
    resolver = KernelPromptResolver(tmp_path)
    prompt = (
        "before\n\n"
        "**Multi-message sends**: old guidance.\n\n"
        "#### after\n"
    )

    resolved = resolver.resolve(
        prompt,
        fragments=(build_prompt_fragment_spec(enabled=False),),
    )

    assert "Multi-message sends" not in resolved
    assert "before" in resolved
    assert "#### after" in resolved


def test_enabled_fragment_requires_kernel_file(tmp_path):
    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(True))

    try:
        policy.resolve(f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n")
    except FileNotFoundError as exc:
        assert SYSTEM_PROMPT_FRAGMENT_PATH in str(exc)
    else:
        raise AssertionError("expected missing fragment file to fail closed")


def test_resolved_prompt_keeps_cache_breakpoint_shape(tmp_path):
    fragment_path = tmp_path / SYSTEM_PROMPT_FRAGMENT_PATH
    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    fragment_path.write_text("fragment text", encoding="utf-8")

    policy = BrainPromptPolicy(kernel_dir=tmp_path, config=_config(True))
    resolved_prompt = policy.resolve(
        f"before\n\n{SYSTEM_PROMPT_PLACEHOLDER}\n\nafter\n"
    )

    builder = ContextBuilder(system_prompt=resolved_prompt, cache_ttl="1h")
    conv = Conversation()
    conv.add("user", "u1")
    conv.add("assistant", "a1")
    conv.add("user", "u2")

    messages = builder.build(conv)

    system_msgs = [m for m in messages if m.role == "system"]
    assert len(system_msgs) == 1
    assert isinstance(system_msgs[0].content, str)
    assert system_msgs[0].cache_control == {"type": "ephemeral", "ttl": "1h"}
    assert "fragment text" in system_msgs[0].content
