"""One-shot, resource-limited PDFium worker.

This module is an internal subprocess entry point for :mod:`turnstone.core.pdf`.
Keep its module-level imports in the standard library: the process applies its
OS resource limits before importing PDFium or Pillow.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import io
import re
import struct
import sys
from pathlib import Path
from typing import BinaryIO, Protocol

EXIT_OK = 0
EXIT_INVALID_PDF = 2
EXIT_RESOURCE_LIMIT = 3
EXIT_UNAVAILABLE = 4

MAX_ADDRESS_SPACE_BYTES = 384 * 1024 * 1024
MAX_CPU_SECONDS = 20
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_RENDER_PX = 2000
MAX_RASTER_PAGES = 10
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_TEXT_PAGES = 100
# Keep the character ceiling derived from the fixed output-byte envelope rather
# than from today's model context sizes. UTF-8 needs at most four bytes per
# Unicode scalar; leave room for the truncation marker.
_TEXT_OUTPUT_MARKER_RESERVE_BYTES = 1024
MAX_TEXT_CHARS = (MAX_FILE_BYTES - _TEXT_OUTPUT_MARKER_RESERVE_BYTES) // 4

_TEXT_CHUNK_CHARS = 64 * 1024
_WHITESPACE_RUN = re.compile(r"\s+|\S+")
_RASTER_LENGTH = struct.Struct("!I")
_RESOURCE_LIMIT_ERRNOS = frozenset(
    getattr(errno, name) for name in ("EFBIG", "ENOMEM", "ENOSPC", "EDQUOT") if hasattr(errno, name)
)


class _ResourceLimitError(RuntimeError):
    pass


class _ResourceModule(Protocol):
    RLIM_INFINITY: int

    def getrlimit(self, resource: int) -> tuple[int, int]: ...

    def setrlimit(self, resource: int, limits: tuple[int, int]) -> None: ...


def _tighten_limit(resource_module: _ResourceModule, name: str, target: int) -> None:
    kind = getattr(resource_module, name)
    soft, hard = resource_module.getrlimit(kind)
    infinity = resource_module.RLIM_INFINITY
    new_hard = target if hard == infinity else min(hard, target)
    new_soft = target if soft == infinity else min(soft, target)
    new_soft = min(new_soft, new_hard)
    resource_module.setrlimit(kind, (new_soft, new_hard))


def _apply_resource_limits() -> None:
    """Apply the hard availability envelope before loading native code.

    Local PDF fallback fails closed on platforms that cannot provide all four
    limits.  The native-PDF path never starts this worker.
    """
    try:
        import resource

        _tighten_limit(resource, "RLIMIT_AS", MAX_ADDRESS_SPACE_BYTES)
        _tighten_limit(resource, "RLIMIT_CPU", MAX_CPU_SECONDS)
        _tighten_limit(resource, "RLIMIT_FSIZE", MAX_FILE_BYTES)
        _tighten_limit(resource, "RLIMIT_CORE", 0)
    except Exception as exc:
        raise _ResourceLimitError("OS resource limits unavailable") from exc


def _append_prefix(
    prefix: io.StringIO,
    prefix_len: int,
    fragment: str,
    max_chars: int,
) -> int:
    remaining = max_chars - prefix_len
    if remaining <= 0:
        return prefix_len
    kept = fragment[:remaining]
    if kept:
        prefix.write(kept)
        prefix_len += len(kept)
    return prefix_len


def _extract_text(input_path: Path, output_path: Path, max_chars: int) -> None:
    import pypdfium2 as pdfium

    doc = None
    try:
        doc = pdfium.PdfDocument(str(input_path))
        prefix = io.StringIO()
        prefix_len = 0
        total_len = 0
        nonempty_pages = 0
        truncated_pages = False

        for page_index, page in enumerate(doc):
            if page_index >= MAX_TEXT_PAGES:
                truncated_pages = True
                page.close()
                break

            textpage = None
            try:
                textpage = page.get_textpage()
                page_started = False
                pending_ws_len = 0
                pending_ws_prefix: list[str] = []
                pending_ws_prefix_len = 0
                count = textpage.count_chars()
                for index in range(0, count, _TEXT_CHUNK_CHARS):
                    chunk = textpage.get_text_range(
                        index,
                        min(_TEXT_CHUNK_CHARS, count - index),
                    )
                    for match in _WHITESPACE_RUN.finditer(chunk):
                        run = match.group(0)
                        if run.isspace():
                            if not page_started:
                                continue
                            pending_ws_len += len(run)
                            remaining = max_chars - prefix_len - pending_ws_prefix_len
                            if remaining > 0:
                                kept = run[:remaining]
                                pending_ws_prefix.append(kept)
                                pending_ws_prefix_len += len(kept)
                            continue

                        if not page_started:
                            page_started = True
                            if nonempty_pages:
                                total_len += 2
                                prefix_len = _append_prefix(
                                    prefix,
                                    prefix_len,
                                    "\n\n",
                                    max_chars,
                                )
                            nonempty_pages += 1
                        elif pending_ws_len:
                            total_len += pending_ws_len
                            if pending_ws_prefix:
                                prefix_len = _append_prefix(
                                    prefix,
                                    prefix_len,
                                    "".join(pending_ws_prefix),
                                    max_chars,
                                )
                            pending_ws_len = 0
                            pending_ws_prefix = []
                            pending_ws_prefix_len = 0

                        total_len += len(run)
                        prefix_len = _append_prefix(prefix, prefix_len, run, max_chars)
                # A pending whitespace run is the page's stripped suffix.
            finally:
                if textpage is not None:
                    with contextlib.suppress(Exception):
                        textpage.close()
                with contextlib.suppress(Exception):
                    page.close()

        if truncated_pages and total_len:
            marker = f"\n\n[PDF truncated at {MAX_TEXT_PAGES} pages]"
            total_len += len(marker)
            prefix_len = _append_prefix(prefix, prefix_len, marker, max_chars)

        text = prefix.getvalue()
        if total_len > max_chars:
            text += f"\n\n... [{total_len - max_chars} chars truncated] ...\n"
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_FILE_BYTES:
            raise _ResourceLimitError("text output exceeds byte cap")
        output_path.write_bytes(encoded)
    finally:
        if doc is not None:
            with contextlib.suppress(Exception):
                doc.close()


def _write_raster_page(output: BinaryIO, png: bytes, written: int) -> int:
    next_written = written + _RASTER_LENGTH.size + len(png)
    if next_written > MAX_FILE_BYTES:
        raise _ResourceLimitError("raster output exceeds byte cap")
    output.write(_RASTER_LENGTH.pack(len(png)))
    output.write(png)
    return next_written


def _write_raster_truncation(output: BinaryIO, written: int) -> None:
    """Write the terminal zero-length frame denoting omitted source pages."""
    if written + _RASTER_LENGTH.size > MAX_FILE_BYTES:
        raise _ResourceLimitError("raster output exceeds byte cap")
    output.write(_RASTER_LENGTH.pack(0))


def _rasterize(
    input_path: Path,
    output_path: Path,
    *,
    max_pages: int,
    scale: float,
) -> None:
    import pypdfium2 as pdfium

    doc = None
    try:
        doc = pdfium.PdfDocument(str(input_path))
        written = 0
        truncated = False
        with output_path.open("wb") as output:
            for page_index, page in enumerate(doc):
                if page_index >= min(max_pages, MAX_RASTER_PAGES):
                    truncated = True
                    page.close()
                    break
                bitmap = None
                image = None
                try:
                    effective_scale = scale
                    try:
                        longest_pt = max(page.get_size())
                        if longest_pt > 0:
                            effective_scale = min(scale, MAX_RENDER_PX / longest_pt)
                    except Exception:
                        effective_scale = min(scale, 1.0)
                    bitmap = page.render(scale=effective_scale)
                    image = bitmap.to_pil()
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    written = _write_raster_page(output, buffer.getvalue(), written)
                finally:
                    if image is not None:
                        with contextlib.suppress(Exception):
                            image.close()
                    if bitmap is not None:
                        with contextlib.suppress(Exception):
                            bitmap.close()
                    with contextlib.suppress(Exception):
                        page.close()
            if truncated:
                _write_raster_truncation(output, written)
    finally:
        if doc is not None:
            with contextlib.suppress(Exception):
                doc.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("operation", choices=("text", "raster"))
    parser.add_argument("input_path", type=Path)
    parser.add_argument("output_path", type=Path)
    parser.add_argument("--max-chars", type=int, default=MAX_TEXT_CHARS)
    parser.add_argument("--max-pages", type=int, default=MAX_RASTER_PAGES)
    parser.add_argument("--scale", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _apply_resource_limits()
        if args.input_path.stat().st_size > MAX_SOURCE_BYTES:
            raise _ResourceLimitError("PDF source exceeds byte cap")
        if args.operation == "text":
            max_chars = min(max(args.max_chars, 0), MAX_TEXT_CHARS)
            _extract_text(args.input_path, args.output_path, max_chars)
        else:
            if args.max_pages < 0 or not (0 < args.scale <= 100):
                return EXIT_INVALID_PDF
            _rasterize(
                args.input_path,
                args.output_path,
                max_pages=args.max_pages,
                scale=args.scale,
            )
        return EXIT_OK
    except _ResourceLimitError:
        return EXIT_RESOURCE_LIMIT
    except MemoryError:
        return EXIT_RESOURCE_LIMIT
    except OSError as exc:
        if exc.errno in _RESOURCE_LIMIT_ERRNOS:
            return EXIT_RESOURCE_LIMIT
        return EXIT_INVALID_PDF
    except ImportError:
        return EXIT_UNAVAILABLE
    except Exception:
        return EXIT_INVALID_PDF


if __name__ == "__main__":  # pragma: no cover - exercised through core.pdf
    sys.exit(main())
