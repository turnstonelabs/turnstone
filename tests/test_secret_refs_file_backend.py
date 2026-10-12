"""The file backend: references resolve only to files under the configured root."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from turnstone.core.secret_refs._errors import SecretBackendError, SecretReferenceError
from turnstone.core.secret_refs._file import FileBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "secrets"
    base.mkdir()
    (base / "openai").write_text("sk-live-123\n")
    return base


class TestFileBackend:
    def test_reads_whole_file_minus_one_trailing_newline(self, root: Path) -> None:
        backend = FileBackend(str(root))
        assert backend.fetch(f"{root}/openai", None) == "sk-live-123"

    def test_strips_exactly_one_newline(self, root: Path) -> None:
        (root / "two").write_text("value\n\n")
        (root / "crlf").write_text("value\r\n")
        (root / "inner").write_text("line1\nline2")
        backend = FileBackend(str(root))
        assert backend.fetch(f"{root}/two", None) == "value\n"
        assert backend.fetch(f"{root}/crlf", None) == "value"
        assert backend.fetch(f"{root}/inner", None) == "line1\nline2"

    def test_field_selector_is_not_supported(self, root: Path) -> None:
        with pytest.raises(SecretReferenceError, match="whole file"):
            FileBackend(str(root)).fetch(f"{root}/openai", "api_key")

    def test_outside_root_is_refused(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "etc-passwd"
        outside.write_text("root:x")
        backend = FileBackend(str(root))
        with pytest.raises(SecretBackendError, match="not under the") as existing:
            backend.fetch(str(outside), None)
        # The answer does not depend on whether the path exists, so a reference
        # cannot be used to map the host's filesystem.
        with pytest.raises(SecretBackendError, match="not under the") as missing:
            backend.fetch(str(tmp_path / "nonexistent" / "id_ed25519"), None)
        assert str(existing.value).replace("etc-passwd", "X") == str(missing.value).replace(
            "nonexistent/id_ed25519", "X"
        )

    def test_symlink_escape_is_refused(self, root: Path, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        outside.write_text("leak")
        os.symlink(outside, root / "link")
        backend = FileBackend(str(root))
        with pytest.raises(SecretBackendError, match="not under the"):
            backend.fetch(f"{root}/link", None)

    def test_escaping_symlinked_directory_answers_alike_for_any_target(
        self, root: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "present").write_text("leak")
        os.symlink(outside, root / "certs")
        backend = FileBackend(str(root))
        with pytest.raises(SecretBackendError, match="not under the") as present:
            backend.fetch(f"{root}/certs/present", None)
        with pytest.raises(SecretBackendError, match="not under the") as absent:
            backend.fetch(f"{root}/certs/absent", None)
        assert str(present.value).replace("present", "X") == str(absent.value).replace(
            "absent", "X"
        )

    def test_root_itself_and_directories_are_refused(self, root: Path) -> None:
        backend = FileBackend(str(root))
        with pytest.raises(SecretBackendError, match="not under the"):
            backend.fetch(str(root), None)
        (root / "dir").mkdir()
        with pytest.raises(SecretBackendError, match="not a file"):
            backend.fetch(f"{root}/dir", None)

    def test_missing_file_is_definitive(self, root: Path) -> None:
        backend = FileBackend(str(root))
        with pytest.raises(SecretBackendError) as info:
            backend.fetch(f"{root}/nope", None)
        assert info.value.retryable is False

    def test_missing_root(self, tmp_path: Path) -> None:
        backend = FileBackend(str(tmp_path / "absent"))
        with pytest.raises(SecretBackendError, match="root is not readable"):
            backend.fetch(str(tmp_path / "absent" / "x"), None)
