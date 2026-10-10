"""The file backend: ``secret://file/<absolute path>`` reads a file under a root."""

from __future__ import annotations

from pathlib import Path

from turnstone.core.secret_refs._errors import SecretBackendError, SecretReferenceError


class FileBackend:
    """Resolve references to files inside the configured root directory.

    The root is the operator's trust boundary: a reference is accepted only
    when the file it names, after symlinks are followed, still lives under it.
    Without that check anyone allowed to edit a model definition or an MCP
    server could make the process read any file it can open and send the
    contents to an endpoint of their choosing. The reference path is the
    file's absolute path (``secret://file/run/secrets/openai``). The whole
    file is the value, minus one trailing newline. Error messages name the path
    from the reference, never the root or the resolved location.
    """

    name = "file"

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def close(self) -> None:
        """Nothing to release: the backend holds no connection."""

    def fetch(self, path: str, key: str | None) -> str:
        if key is not None:
            raise SecretReferenceError(
                "a file reference names a whole file; '#field' is not supported "
                "(secret://file/<absolute path>)"
            )
        try:
            root = self._root.resolve(strict=True)
        except OSError as exc:
            raise SecretBackendError(
                f"the [secrets.file] root is not readable: {exc.strerror or exc}", retryable=False
            ) from exc
        try:
            target = Path(path).resolve(strict=True)
        except OSError as exc:
            raise SecretBackendError(
                f"{path}: {exc.strerror or 'cannot be resolved'}", retryable=False
            ) from exc
        if target == root or not target.is_relative_to(root):
            raise SecretBackendError(f"{path}: not under the [secrets.file] root", retryable=False)
        if not target.is_file():
            raise SecretBackendError(f"{path}: not a file", retryable=False)
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SecretBackendError(
                f"{path}: cannot be read ({type(exc).__name__})", retryable=False
            ) from exc
        # Mounted secret files usually end with one newline that is not part of
        # the value; everything else is kept verbatim.
        return text.removesuffix("\n").removesuffix("\r")
