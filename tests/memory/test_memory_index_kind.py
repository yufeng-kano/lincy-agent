from lincy.memory.index_kind import IndexKind, classify_memory_index_path


def test_classify_people_root_index_as_registry() -> None:
    assert classify_memory_index_path("memory/people/index.md") == IndexKind.REGISTRY


def test_classify_nested_people_index_as_nav() -> None:
    assert classify_memory_index_path("memory/people/alice/index.md") == IndexKind.NAV


def test_non_index_returns_none() -> None:
    assert classify_memory_index_path("memory/people/alice/basic-info.md") is None
