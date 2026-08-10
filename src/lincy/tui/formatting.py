"""Small shared formatting helpers for UI renderers."""


def indent_lines(text: str, prefix: str = "  ") -> str:
    """Indent every line, preserving an empty block as one blank line."""
    lines = text.splitlines() or [""]
    return "\n".join(f"{prefix}{line}" for line in lines)
