"""Resource-bounded PDF helpers.

Text extraction for the no-native-PDF fallback: when a model lacks
``supports_pdf``, the wire resolver extracts the PDF's text here and sends it as
a text document rather than PDF bytes the model can't read. Native PDFium work
runs in a one-shot child with OS memory/CPU/file limits plus a parent wall
deadline. The worker is an availability boundary, not a security sandbox.

Re-run per wire build by design — there is intentionally no module-global cache
here.  A PDF re-parsed on every turn of a long conversation is wasteful, but the
principled place to memoize a *derived representation of a content-addressed
blob* is a durable derived-artifact store keyed by (source-hash, derivation)
that would serve every kind uniformly — not a per-module dict that happens to
hold PDFs.  See the attachments design brief; that store is deferred.
"""

from __future__ import annotations

import contextlib
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from turnstone.core._pdf_worker import (
    EXIT_INVALID_PDF,
    EXIT_OK,
    EXIT_RESOURCE_LIMIT,
    EXIT_UNAVAILABLE,
    MAX_FILE_BYTES,
    MAX_RASTER_PAGES,
    MAX_SOURCE_BYTES,
    MAX_TEXT_CHARS,
)
from turnstone.core.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

log = get_logger(__name__)

_MAX_WALL_SECONDS = 30.0
_POLL_SECONDS = 0.05
_RASTER_LENGTH = struct.Struct("!I")
_PDF_WORKER_PATH = Path(__file__).with_name("_pdf_worker.py").resolve(strict=True)

# Public safety ceiling for callers that layer a narrower presentation budget
# over the worker. It is derived from the worker's fixed output-byte envelope,
# not from any particular model generation's context window.
PDF_TEXT_CHAR_CAP = MAX_TEXT_CHARS
PDF_RASTER_PAGE_CAP = MAX_RASTER_PAGES

# A single slot turns the per-child address-space ceiling into an aggregate
# process bound. Queue time shares the same wall deadline as execution.
_PDF_WORKER_SLOT = threading.BoundedSemaphore(1)


class PdfWorkLimitError(RuntimeError):
    """Local PDF work could not complete inside its safety envelope."""


class PdfRasterizedPages(list[bytes]):
    """Rendered pages plus whether more source pages were deliberately omitted.

    This remains list-compatible for existing thumbnail and attachment callers;
    the explicit flag lets model-facing materialization disclose the fixed page
    cutoff instead of silently presenting a partial document as complete.
    """

    def __init__(self, pages: Iterable[bytes] = (), *, truncated: bool = False) -> None:
        super().__init__(pages)
        self.truncated = truncated


def _check_cancelled(check_cancelled: Callable[[], None] | None) -> None:
    if check_cancelled is not None:
        check_cancelled()


def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with contextlib.suppress(Exception):
        process.kill()
    with contextlib.suppress(Exception):
        process.wait(timeout=1)


class _PdfInvalidError(Exception):
    pass


class _PdfBackendUnavailableError(Exception):
    pass


def _worker_bytes(
    data: bytes,
    operation: str,
    arguments: list[str],
    *,
    check_cancelled: Callable[[], None] | None,
) -> bytes:
    """Run one worker and return its size-validated result bytes."""
    if len(data) > MAX_SOURCE_BYTES:
        raise PdfWorkLimitError("PDF source exceeds local processing cap")

    deadline = time.monotonic() + _MAX_WALL_SECONDS
    acquired = False
    process: subprocess.Popen[bytes] | None = None
    try:
        while not acquired:
            _check_cancelled(check_cancelled)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PdfWorkLimitError("PDF worker queue deadline exceeded")
            acquired = _PDF_WORKER_SLOT.acquire(timeout=min(_POLL_SECONDS, remaining))

        try:
            temp_context = tempfile.TemporaryDirectory(prefix="turnstone-pdf-")
        except OSError as exc:
            raise PdfWorkLimitError("PDF worker directory could not be created") from exc
        with temp_context as temp_name:
            root = Path(temp_name)
            input_path = root / "input.pdf"
            output_path = root / "output.bin"
            try:
                input_path.write_bytes(data)
            except OSError as exc:
                raise PdfWorkLimitError("PDF worker input could not be created") from exc
            _check_cancelled(check_cancelled)
            if time.monotonic() >= deadline:
                raise PdfWorkLimitError("PDF worker deadline exceeded")

            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        # Keep the worker's trusted absolute entry point and
                        # private cwd out of the import path while retaining
                        # ordinary user-site installations of pypdfium2/Pillow.
                        # ``-E`` rejects PYTHONPATH/PYTHONHOME injection and
                        # ``-P`` suppresses the unsafe script/cwd prepend. Do
                        # not use ``-I``: its implied ``-s`` breaks supported
                        # ``pip install --user`` environments.
                        "-E",
                        "-P",
                        str(_PDF_WORKER_PATH),
                        operation,
                        str(input_path),
                        str(output_path),
                        *arguments,
                    ],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=os.name == "posix",
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise PdfWorkLimitError("PDF worker could not be started") from exc
            while process.poll() is None:
                _check_cancelled(check_cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PdfWorkLimitError("PDF worker wall deadline exceeded")
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=min(_POLL_SECONDS, remaining))
            _check_cancelled(check_cancelled)

            if process.returncode == EXIT_INVALID_PDF:
                raise _PdfInvalidError
            if process.returncode == EXIT_UNAVAILABLE:
                raise _PdfBackendUnavailableError
            if process.returncode == EXIT_RESOURCE_LIMIT:
                raise PdfWorkLimitError("PDF worker exceeded its safety envelope")
            if process.returncode != EXIT_OK:
                raise PdfWorkLimitError(
                    f"PDF worker exited outside its safety envelope ({process.returncode})"
                )
            try:
                output_size = output_path.stat().st_size
            except OSError as exc:
                raise PdfWorkLimitError("PDF worker produced no result") from exc
            if output_size > MAX_FILE_BYTES:
                raise PdfWorkLimitError("PDF worker result exceeds output cap")
            try:
                return output_path.read_bytes()
            except OSError as exc:
                raise PdfWorkLimitError("PDF worker result could not be read") from exc
    except BaseException:
        if process is not None:
            _terminate_worker(process)
        raise
    finally:
        if acquired:
            _PDF_WORKER_SLOT.release()


def extract_pdf_text(
    data: bytes,
    *,
    max_chars: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> str:
    """Best-effort bounded text from a PDF.

    Returns ``""`` on a parse failure or a scanned PDF with no text layer.  Walks
    at most 100 pages. Resource-envelope exhaustion raises
    :class:`PdfWorkLimitError`; cancellation callbacks propagate unchanged.
    """
    try:
        limit = MAX_TEXT_CHARS if max_chars is None else min(max(max_chars, 0), MAX_TEXT_CHARS)
        output = _worker_bytes(
            data,
            "text",
            ["--max-chars", str(limit)],
            check_cancelled=check_cancelled,
        )
        return output.decode("utf-8")
    except (_PdfInvalidError, UnicodeDecodeError):
        log.warning("PDF text extraction failed")
        return ""
    except _PdfBackendUnavailableError:
        log.warning("pypdfium2 not installed; PDF text extraction unavailable")
        return ""


# Bound page count + payload for the rasterize fallback (images are far heavier
# than text).  Re-run per wire build — same no-cache rationale as extract_pdf_text.
def rasterize_pdf(
    data: bytes,
    *,
    max_pages: int = MAX_RASTER_PAGES,
    scale: float = 2.0,
    check_cancelled: Callable[[], None] | None = None,
) -> PdfRasterizedPages:
    """Render up to ``max_pages`` PDF pages to PNG bytes, one per page.

    For vision-capable models that can't read PDF natively. The returned
    list-compatible object sets ``truncated`` when another source page was
    observed beyond the limit. Returns an empty result on a parse/render
    failure. Resource-envelope exhaustion raises
    :class:`PdfWorkLimitError`; cancellation callbacks propagate unchanged.
    """
    if max_pages < 0 or not (0 < scale <= 100):
        return PdfRasterizedPages()
    page_limit = min(max_pages, MAX_RASTER_PAGES)
    try:
        output = _worker_bytes(
            data,
            "raster",
            [
                "--max-pages",
                str(page_limit),
                "--scale",
                str(scale),
            ],
            check_cancelled=check_cancelled,
        )
        pages: list[bytes] = []
        truncated = False
        offset = 0
        while offset < len(output):
            if len(output) - offset < _RASTER_LENGTH.size:
                raise PdfWorkLimitError("PDF worker returned malformed raster data")
            (length,) = _RASTER_LENGTH.unpack_from(output, offset)
            offset += _RASTER_LENGTH.size
            # A zero-length terminal frame is the worker's explicit signal
            # that it observed another source page beyond ``page_limit``.
            if length == 0:
                if offset != len(output):
                    raise PdfWorkLimitError("PDF worker returned malformed raster data")
                truncated = True
                break
            if len(pages) >= page_limit:
                raise PdfWorkLimitError("PDF worker returned too many raster pages")
            end = offset + length
            if end > len(output):
                raise PdfWorkLimitError("PDF worker returned malformed raster data")
            page = output[offset:end]
            if not page.startswith(b"\x89PNG\r\n\x1a\n"):
                raise PdfWorkLimitError("PDF worker returned malformed PNG data")
            pages.append(page)
            offset = end
        return PdfRasterizedPages(pages, truncated=truncated)
    except _PdfInvalidError:
        log.warning("PDF rasterize failed")
        return PdfRasterizedPages()
    except _PdfBackendUnavailableError:
        log.warning("pypdfium2 not installed; PDF rasterize unavailable")
        return PdfRasterizedPages()
