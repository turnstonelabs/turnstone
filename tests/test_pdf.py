"""Tests for core.pdf text extraction (the no-native-PDF wire fallback)."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from turnstone.core import _pdf_worker
from turnstone.core import pdf as pdf_ops
from turnstone.core.pdf import (
    PdfHostUnavailableError,
    PdfWorkLimitError,
    extract_pdf_text,
    rasterize_pdf,
)


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

    def test_refused_limits_exit_is_a_terminal_unavailable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class LimitsRefusedProcess:
            returncode = _pdf_worker.EXIT_LIMITS_UNAVAILABLE

            def poll(self):
                return self.returncode

        monkeypatch.setattr(
            pdf_ops.subprocess,
            "Popen",
            lambda *args, **kwargs: LimitsRefusedProcess(),
        )
        log = MagicMock()
        monkeypatch.setattr(pdf_ops, "log", log)

        with pytest.raises(PdfHostUnavailableError, match="refuses") as excinfo:
            extract_pdf_text(_minimal_pdf())
        # A work-limit subclass, so every handler that treats limits as terminal still does.
        assert isinstance(excinfo.value, PdfWorkLimitError)
        log.warning.assert_called_once_with("pdf.worker_unavailable", step="limits", error=ANY)

    @pytest.mark.parametrize("broken", ["TemporaryDirectory", "Popen"])
    @pytest.mark.parametrize(
        "error",
        [
            PermissionError(errno.EACCES, "unwritable"),
            OSError(errno.EROFS, "read-only file system"),
            FileNotFoundError(errno.ENOENT, "missing"),
            # Exhaustion too: the PDF hasn't been touched, so it isn't over a limit.
            OSError(errno.ENOSPC, "no space left"),
            OSError(errno.EMFILE, "too many open files"),
            OSError(errno.EAGAIN, "resource temporarily unavailable"),
        ],
        ids=["EACCES", "EROFS", "ENOENT", "ENOSPC", "EMFILE", "EAGAIN"],
    )
    def test_a_worker_that_cannot_be_launched_means_the_host_is_unavailable(
        self,
        monkeypatch: pytest.MonkeyPatch,
        broken: str,
        error: OSError,
    ) -> None:
        def fail(*_args, **_kwargs):
            raise error

        target = pdf_ops.tempfile if broken == "TemporaryDirectory" else pdf_ops.subprocess
        monkeypatch.setattr(target, broken, fail)
        log = MagicMock()
        monkeypatch.setattr(pdf_ops, "log", log)

        with pytest.raises(PdfHostUnavailableError):
            extract_pdf_text(_minimal_pdf())
        step = "directory" if broken == "TemporaryDirectory" else "start"
        log.warning.assert_called_once_with("pdf.worker_unavailable", step=step, error=str(error))

    def test_a_failed_input_write_stays_a_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_write_bytes = Path.write_bytes

        def write_bytes(path: Path, data: bytes) -> int:
            if path.name == "input.pdf":
                raise OSError(errno.ENOSPC, "no space left")
            return real_write_bytes(path, data)

        monkeypatch.setattr(Path, "write_bytes", write_bytes)

        with pytest.raises(PdfWorkLimitError) as excinfo:
            extract_pdf_text(_minimal_pdf())

        # The PDF's own size can fill the disk, so this step is not the host's fault.
        assert not isinstance(excinfo.value, PdfHostUnavailableError)

    def test_a_failed_cleanup_does_not_replace_the_classified_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        monkeypatch.setattr(pdf_ops.tempfile, "tempdir", str(tmp_path))
        real_rmtree = shutil.rmtree

        def rmtree_out_of_fds(path, *args, onexc=None, **kwargs):
            # What the real rmtree does when it can't open the directory: report
            # the error to onexc from inside its own handler.
            try:
                raise OSError(errno.EMFILE, "Too many open files", path)
            except OSError as err:
                onexc(os.open, path, err)
            real_rmtree(path)

        def spawn_fails(*_args, **_kwargs):
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(shutil, "rmtree", rmtree_out_of_fds)
        monkeypatch.setattr(pdf_ops.subprocess, "Popen", spawn_fails)

        with pytest.raises(PdfHostUnavailableError):
            extract_pdf_text(_minimal_pdf())

    @pytest.mark.parametrize("stop", ["cancelled", "deadline"])
    def test_a_running_worker_is_killed_before_its_directory_is_removed(
        self, monkeypatch: pytest.MonkeyPatch, stop: str
    ) -> None:
        class Cancelled(BaseException):
            pass

        class RunningProcess:
            returncode = None
            killed = False
            directory_at_kill = False

            def __init__(self, directory: Path) -> None:
                self.directory = directory

            def poll(self):
                return -9 if self.killed else None

            def wait(self, timeout=None):
                if self.killed:
                    self.returncode = -9
                    return self.returncode
                raise subprocess.TimeoutExpired("pdf-worker", timeout)

            def kill(self):
                self.killed = True
                self.directory_at_kill = self.directory.is_dir()

        clock = SimpleNamespace(now=0.0)
        monkeypatch.setattr(pdf_ops, "time", SimpleNamespace(monotonic=lambda: clock.now))
        launched: list[RunningProcess] = []

        def launch(*_args, cwd, **_kwargs) -> RunningProcess:
            if stop == "deadline":
                clock.now = 1e9
            launched.append(RunningProcess(Path(cwd)))
            return launched[-1]

        monkeypatch.setattr(pdf_ops.subprocess, "Popen", launch)

        def check_cancelled() -> None:
            if stop == "cancelled" and launched:
                raise Cancelled

        with pytest.raises(
            Cancelled if stop == "cancelled" else PdfWorkLimitError,
            match=None if stop == "cancelled" else "wall deadline exceeded",
        ):
            extract_pdf_text(_minimal_pdf(), check_cancelled=check_cancelled)

        [process] = launched
        assert process.killed
        # Killed while its files were still there, not after the cleanup removed them.
        assert process.directory_at_kill
        assert not process.directory.exists()


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


_OS_ENVELOPE = [
    ("RLIMIT_AS", _pdf_worker.MAX_ADDRESS_SPACE_BYTES),
    ("RLIMIT_CPU", _pdf_worker.MAX_CPU_SECONDS),
    ("RLIMIT_FSIZE", _pdf_worker.MAX_FILE_BYTES),
    ("RLIMIT_CORE", 0),
]


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

        def apply_limits(*, memory_supervised: bool) -> None:
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

        assert applied == _OS_ENVELOPE

    @pytest.mark.parametrize(
        ("current", "expected"),
        [
            ((-1, -1), (20, 20)),  # unlimited becomes the target
            ((30, -1), (20, 20)),  # a looser soft limit is lowered too
            ((10, 15), (10, 15)),  # stricter inherited limits are kept
            ((30, 40), (20, 20)),  # a looser finite hard limit is lowered too
        ],
        ids=["unlimited", "looser", "stricter", "looser-hard"],
    )
    def test_tighten_limit_lowers_each_limit_to_the_target(
        self,
        current: tuple[int, int],
        expected: tuple[int, int],
    ) -> None:
        calls: list[tuple[int, tuple[int, int]]] = []

        class FakeResource:
            # Linux spells "unlimited" as -1, below every finite target.
            RLIM_INFINITY = -1
            # Not a real resource: a call that reaches the real setrlimit raises
            # instead of limiting this process.
            RLIMIT_TEST = 10_000

            def getrlimit(self, kind: int) -> tuple[int, int]:
                return current

            def setrlimit(self, kind: int, limits: tuple[int, int]) -> None:
                calls.append((kind, limits))

        _pdf_worker._tighten_limit(FakeResource(), "RLIMIT_TEST", 20)

        assert calls == [(10_000, expected)]

    @pytest.mark.parametrize("refused", [name for name, _target in _OS_ENVELOPE])
    @pytest.mark.parametrize("operation", ["text", "raster"])
    def test_a_refused_os_limit_stops_the_worker_before_it_reads_the_pdf(
        self,
        operation: str,
        refused: str,
        tmp_path,
    ) -> None:
        # macOS refuses any finite RLIMIT_AS, so this is the worker's normal path there. The
        # OS call is faked in a child process, so the real limits never land on the test
        # process. The child runs the worker by path as __main__, as core.pdf does (under -I
        # a package import resolves through the venv, which in a worktree can be another
        # checkout), and any import of PDFium or Pillow, before or after the refusal, ends it.
        script = """\
import os
import resource
import runpy
import sys

worker_path, operation, refused, input_path, output_path = sys.argv[1:]
refused_kind = getattr(resource, refused)


def setrlimit(kind, limits):
    # An OS that refuses a cap still accepts leaving the limit unlimited.
    if kind == refused_kind and limits[0] != resource.RLIM_INFINITY:
        raise ValueError("current limit exceeds maximum limit")


class ParserSentinel:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name.partition(".")[0] in ("pypdfium2", "pypdfium2_raw", "PIL"):
            os.write(2, f"{name} was imported in a run that refuses {refused}".encode())
            os._exit(99)
        return None


resource.setrlimit = setrlimit
sys.meta_path.insert(0, ParserSentinel)
sys.argv = [worker_path, operation, input_path, output_path]
runpy.run_path(worker_path, run_name="__main__")
"""
        input_path = tmp_path / "input.pdf"
        input_path.write_bytes(_minimal_pdf())

        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                script,
                str(pdf_ops._PDF_WORKER_PATH),
                operation,
                refused,
                str(input_path),
                str(tmp_path / "output.bin"),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == _pdf_worker.EXIT_LIMITS_UNAVAILABLE, result.stderr

    def test_memory_supervised_worker_hands_only_address_space_to_parent(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        applied: list[str] = []
        monkeypatch.setattr(
            _pdf_worker, "_tighten_limit", lambda _resource, name, _target: applied.append(name)
        )

        _pdf_worker._apply_resource_limits(memory_supervised=True)

        assert applied == ["RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_CORE"]

    @pytest.mark.parametrize(
        "error_name",
        [
            name
            for name in ("ENOSPC", "EDQUOT", "EFBIG", "ENOMEM", "EAGAIN", "EMFILE", "ENFILE")
            if hasattr(errno, name)
        ],
    )
    def test_resource_exhaustion_is_a_resource_limit(
        self,
        error_name: str,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path,
    ) -> None:
        error_code = getattr(errno, error_name)

        monkeypatch.setattr(_pdf_worker, "_apply_resource_limits", lambda **_: None)

        def resource_exhausted(*_args, **_kwargs) -> None:
            raise OSError(error_code, "worker ran out of a resource")

        monkeypatch.setattr(_pdf_worker, "_extract_text", resource_exhausted)
        input_path = tmp_path / "input.pdf"
        output_path = tmp_path / "output.bin"
        input_path.write_bytes(_minimal_pdf())

        assert (
            _pdf_worker.main(["text", str(input_path), str(output_path)])
            == _pdf_worker.EXIT_RESOURCE_LIMIT
        )


class TestParentMemorySupervision:
    def test_footprint_over_ceiling_kills_worker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def footprint(pid: int) -> int:
            seen.append(pid)
            return _pdf_worker.MAX_ADDRESS_SPACE_BYTES + 1

        monkeypatch.setattr(pdf_ops, "_worker_footprint", footprint)
        started: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def popen(argv, **kwargs):
            started.append(real_popen([sys.executable, "-c", "import time; time.sleep(30)"]))
            assert argv[-1] == "--memory-supervised"
            return started[-1]

        monkeypatch.setattr(pdf_ops.subprocess, "Popen", popen)

        with pytest.raises(PdfWorkLimitError, match="safety envelope"):
            extract_pdf_text(_minimal_pdf())

        assert seen == [started[0].pid]
        assert started[0].poll() is not None

    def test_unmeasurable_live_worker_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pdf_ops, "_worker_footprint", lambda _pid: None)
        started: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen

        def popen(_argv, **_kwargs):
            started.append(real_popen([sys.executable, "-c", "import time; time.sleep(30)"]))
            return started[-1]

        monkeypatch.setattr(pdf_ops.subprocess, "Popen", popen)

        with pytest.raises(PdfWorkLimitError, match="could not be measured"):
            extract_pdf_text(_minimal_pdf())

        assert started[0].poll() is not None

    @pytest.mark.skipif(sys.platform != "darwin", reason="exercises the macOS footprint reader")
    def test_real_worker_over_ceiling_is_killed_on_macos(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pdf_ops, "MAX_ADDRESS_SPACE_BYTES", 1)

        with pytest.raises(PdfWorkLimitError, match="safety envelope"):
            extract_pdf_text(_minimal_pdf())

    def test_unsupervised_worker_still_fails_closed_without_rlimit_as(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        def tighten(_resource, name: str, _target: int) -> None:
            if name == "RLIMIT_AS":
                raise ValueError("current limit exceeds maximum limit")

        monkeypatch.setattr(_pdf_worker, "_tighten_limit", tighten)
        input_path = tmp_path / "input.pdf"
        input_path.write_bytes(_minimal_pdf())

        assert (
            _pdf_worker.main(["text", str(input_path), str(tmp_path / "out.bin")])
            == _pdf_worker.EXIT_LIMITS_UNAVAILABLE
        )
