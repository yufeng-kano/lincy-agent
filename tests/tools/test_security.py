"""Tests for path security utilities."""

from pathlib import Path


from lincy.tools.security import is_path_allowed


class TestIsPathAllowed:
    def test_absolute_path_within_base(self, tmp_path: Path):
        """Absolute path within base_dir is allowed."""
        base = tmp_path / "workspace"
        base.mkdir()
        target = base / "file.txt"

        assert is_path_allowed(str(target), [], base) is True

    def test_absolute_path_outside_base(self, tmp_path: Path):
        """Absolute path outside base_dir is denied when no allowed_paths."""
        base = tmp_path / "workspace"
        base.mkdir()
        outside = tmp_path / "outside" / "file.txt"

        assert is_path_allowed(str(outside), [], base) is False

    def test_relative_path_resolved(self, tmp_path: Path):
        """Relative paths are resolved against base_dir."""
        base = tmp_path / "workspace"
        base.mkdir()

        assert is_path_allowed("file.txt", [], base) is True
        assert is_path_allowed("subdir/file.txt", [], base) is True

    def test_traversal_attempt_blocked(self, tmp_path: Path):
        """Path traversal attempts are blocked."""
        base = tmp_path / "workspace"
        base.mkdir()

        # Attempt to escape via ..
        assert is_path_allowed("../outside.txt", [], base) is False
        assert is_path_allowed("subdir/../../outside.txt", [], base) is False

    def test_allowed_paths_permits_outside(self, tmp_path: Path):
        """Paths in allowed_paths are permitted."""
        base = tmp_path / "workspace"
        base.mkdir()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        target = allowed / "file.txt"

        assert is_path_allowed(str(target), [str(allowed)], base) is True

    def test_allowed_paths_denies_other(self, tmp_path: Path):
        """Paths not in allowed_paths are denied."""
        base = tmp_path / "workspace"
        base.mkdir()
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        denied = tmp_path / "denied"
        denied.mkdir()
        target = denied / "file.txt"

        assert is_path_allowed(str(target), [str(allowed)], base) is False

    def test_allowed_paths_tilde_expansion(self, tmp_path: Path):
        """Tilde in allowed_paths is expanded."""
        base = tmp_path / "workspace"
        base.mkdir()

        # Home directory should be expanded
        home = Path.home()
        target = home / "some_file.txt"

        assert is_path_allowed(str(target), ["~"], base) is True

    def test_multiple_allowed_paths(self, tmp_path: Path):
        """Multiple allowed_paths are all checked."""
        base = tmp_path / "workspace"
        base.mkdir()
        allowed1 = tmp_path / "allowed1"
        allowed1.mkdir()
        allowed2 = tmp_path / "allowed2"
        allowed2.mkdir()
        denied = tmp_path / "denied"
        denied.mkdir()

        # Both allowed paths work
        assert is_path_allowed(str(allowed1 / "f.txt"), [str(allowed1), str(allowed2)], base) is True
        assert is_path_allowed(str(allowed2 / "f.txt"), [str(allowed1), str(allowed2)], base) is True

        # Denied path still fails
        assert is_path_allowed(str(denied / "f.txt"), [str(allowed1), str(allowed2)], base) is False
