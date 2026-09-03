"""Tests for core.pdf text extraction (the no-native-PDF wire fallback)."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from turnstone.core import _pdf_worker
from turnstone.core import pdf as pdf_ops
from turnstone.core.pdf import PdfWorkLimitError, extract_pdf_text, rasterize_pdf


def _minimal_pdf(text: str = "Hello PDF") -> bytes:
    """A valid one-page PDF with a single text line (xref offsets computed)."""
    stream = b"BT /F1 24 Tf 20 60 Td (" + text.encode("latin-1") + b") Tj ET"
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]"
        + b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length %d>>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        pdf += b"%010d 00000 n \n" % off
    pdf += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return pdf


def _blank_pdf(page_count: int) -> bytes:
    """A valid small PDF with ``page_count`` blank pages."""
    page_ids = range(3, 3 + page_count)
    kids = b" ".join(f"{page_id} 0 R".encode() for page_id in page_ids)
    objs = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[" + kids + b"]/Count %d>>" % page_count,
        *[
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 10 10]/Resources<<>>>>"
            for _ in range(page_count)
        ],
    ]
    pdf = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objs, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n%s\nendobj\n" % (index, obj)
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (
        len(objs) + 1,
        xref,
    )
    return pdf


class TestExtractPdfText:
    def test_extracts_text(self) -> None:
        assert "Hello PDF" in extract_pdf_text(_minimal_pdf("Hello PDF"))

    def test_garbage_returns_empty_no_raise(self) -> None:
        assert extract_pdf_text(b"not a pdf at all") == ""

    def test_empty_returns_empty(self) -> None:
        assert extract_pdf_text(b"") == ""

    def test_character_cap_is_applied_inside_worker(self) -> None:
        assert extract_pdf_text(_minimal_pdf("abcdefghij"), max_chars=4) == "abc…"

    def test_character_marker_fits_inside_nontrivial_cap(self) -> None:
        result = extract_pdf_text(_minimal_pdf("x" * 100), max_chars=64)

        assert len(result) <= 64
        assert result.startswith("x")
        assert "chars truncated" in result

    def test_resource_exit_is_typed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class ResourceLimitedProcess:
            returncode = _pdf_worker.EXIT_RESOURCE_LIMIT

            def poll(self):
                return self.returncode

        monkeypatch.setattr(
            pdf_ops.subprocess,
            "Popen",
            lambda *args, **kwargs: ResourceLimitedProcess(),
        )

        with pytest.raises(PdfWorkLimitError, match="safety envelope"):
            extract_pdf_text(_minimal_pdf())

    def test_cancellation_kills_inflight_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Cancelled(BaseException):
            pass

        class RunningProcess:
            returncode = None
            killed = False

            def poll(self):
                return -9 if self.killed else None

            def wait(self, timeout=None):
                if self.killed:
                    self.returncode = -9
                    return self.returncode
                raise subprocess.TimeoutExpired("pdf-worker", timeout)

            def kill(self):
                self.killed = True

        process = RunningProcess()
        monkeypatch.setattr(
            pdf_ops.subprocess,
            "Popen",
            lambda *args, **kwargs: process,
        )
        checks = 0

        def check_cancelled() -> None:
            nonlocal checks
            checks += 1
            if checks >= 3:
                raise Cancelled

        with pytest.raises(Cancelled):
            extract_pdf_text(_minimal_pdf(), check_cancelled=check_cancelled)

        assert process.killed


class TestTextHeaderProtocol:
    def test_parent_renders_the_marker_from_the_worker_prefix(self) -> None:
        assert extract_pdf_text(_minimal_pdf("abcdefghij"), max_chars=8) == "abcdefg…"

    def test_zero_allowance_returns_the_marker_as_a_receipt(self) -> None:
        result = extract_pdf_text(_minimal_pdf("abcdefghij"), max_chars=0)

        assert result == "\n\n... [10 chars truncated] ...\n"

    def test_marker_counts_the_true_size(self) -> None:
        from turnstone.core.pdf import _render_pdf_text

        text = _render_pdf_text("a" * 1000, 123456, 1000)

        head, _, rest = text.partition("\n\n... [")
        assert len(text) == 1000
        assert int(rest.split(" ")[0]) == 123456 - len(head)

    def test_short_worker_output_reads_as_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("turnstone.core.pdf._worker_bytes", lambda *a, **k: b"abc")

        assert extract_pdf_text(_minimal_pdf("abcdefghij")) == ""


class TestRasterizePdf:
    def test_renders_pages_to_png(self) -> None:
        pages = rasterize_pdf(_minimal_pdf("Hello PDF"))
        assert len(pages) == 1
        assert pages[0][:8] == b"\x89PNG\r\n\x1a\n"
        assert not pages.truncated

    def test_reports_when_source_has_pages_beyond_raster_limit(self) -> None:
        pages = rasterize_pdf(_blank_pdf(_pdf_worker.MAX_RASTER_PAGES + 1))

        assert len(pages) == _pdf_worker.MAX_RASTER_PAGES
        assert pages.truncated

    def test_garbage_returns_empty_no_raise(self) -> None:
        pages = rasterize_pdf(b"not a pdf at all")
        assert pages == []
        assert not pages.truncated


class TestPdfWorkerEnvelope:
    def test_text_character_ceiling_is_derived_from_output_bytes(self) -> None:
        expected = (_pdf_worker.MAX_FILE_BYTES - _pdf_worker._TEXT_OUTPUT_HEADER_RESERVE_BYTES) // 4
        assert expected == _pdf_worker.MAX_TEXT_CHARS

    def test_worker_retains_user_site_dependencies(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        base_executable = getattr(sys, "_base_executable", sys.executable)
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("PYTHONUSERBASE", raising=False)
        env = os.environ.copy()
        user_site = subprocess.check_output(
            [
                base_executable,
                "-E",
                "-P",
                "-c",
                "import site; print(site.getusersitepackages())",
            ],
            env=env,
            text=True,
        ).strip()
        dependency_root = tmp_path / "home" / ".local"
        assert user_site.startswith(str(dependency_root))
        site_path = Path(user_site)
        site_path.mkdir(parents=True)
        expected = "loaded from user site"
        (site_path / "pypdfium2.py").write_text(
            f"""\
TEXT = {expected!r}

class _TextPage:
    def count_chars(self):
        return len(TEXT)
    def get_text_range(self, index, count):
        return TEXT[index:index + count]
    def close(self):
        pass

class _Page:
    def get_textpage(self):
        return _TextPage()
    def close(self):
        pass

class PdfDocument:
    def __init__(self, _path):
        self.pages = [_Page()]
    def __iter__(self):
        return iter(self.pages)
    def close(self):
        pass
""",
            encoding="utf-8",
        )
        monkeypatch.setattr(pdf_ops.sys, "executable", base_executable)

        assert extract_pdf_text(b"%PDF-user-site") == expected

    def test_all_required_os_limits_are_applied_before_operation(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        events: list[str] = []

        def apply_limits() -> None:
            events.append("limits")

        def extract(input_path, output_path, max_chars) -> None:
            assert max_chars == 12
            events.append("extract")
            output_path.write_bytes(b"bounded")

        monkeypatch.setattr(_pdf_worker, "_apply_resource_limits", apply_limits)
        monkeypatch.setattr(_pdf_worker, "_extract_text", extract)
        input_path = tmp_path / "input.pdf"
        output_path = tmp_path / "output.bin"
        input_path.write_bytes(_minimal_pdf())

        exit_code = _pdf_worker.main(
            ["text", str(input_path), str(output_path), "--max-chars", "12"]
        )

        assert exit_code == _pdf_worker.EXIT_OK
        assert events == ["limits", "extract"]
        assert output_path.read_bytes() == b"bounded"

    def test_os_envelope_contains_memory_cpu_output_and_core_limits(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        applied: list[tuple[str, int]] = []

        def tighten(_resource, name: str, target: int) -> None:
            applied.append((name, target))

        monkeypatch.setattr(_pdf_worker, "_tighten_limit", tighten)

        _pdf_worker._apply_resource_limits()

        assert applied == [
            ("RLIMIT_AS", _pdf_worker.MAX_ADDRESS_SPACE_BYTES),
            ("RLIMIT_CPU", _pdf_worker.MAX_CPU_SECONDS),
            ("RLIMIT_FSIZE", _pdf_worker.MAX_FILE_BYTES),
            ("RLIMIT_CORE", 0),
        ]

    @pytest.mark.parametrize(
        "error_name",
        [name for name in ("ENOSPC", "EDQUOT") if hasattr(errno, name)],
    )
    def test_storage_exhaustion_is_a_resource_limit(
        self,
        error_name: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        error_code = getattr(errno, error_name)

        monkeypatch.setattr(_pdf_worker, "_apply_resource_limits", lambda: None)

        def storage_full(*_args, **_kwargs) -> None:
            raise OSError(error_code, "worker output storage exhausted")

        monkeypatch.setattr(_pdf_worker, "_extract_text", storage_full)
        input_path = tmp_path / "input.pdf"
        output_path = tmp_path / "output.bin"
        input_path.write_bytes(_minimal_pdf())

        assert (
            _pdf_worker.main(["text", str(input_path), str(output_path)])
            == _pdf_worker.EXIT_RESOURCE_LIMIT
        )
