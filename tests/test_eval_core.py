"""Per-run resource lifecycle for ``turnstone.eval.core._run_single_test``.

The sibling harness (``eval.nudges._run_single_nudge``) carries the
canonical statement of why the teardown is shaped the way it is; these
tests pin the same properties on this side, one test per mechanism, so
reverting any one of them fails its own test and no others.
"""

import os
import shutil
import tempfile
import time
from typing import Any

from turnstone.core.storage import is_storage_initialized, reset_storage


class _Params:
    """The only two attributes ``_run_single_test`` reads off ``client``.

    A real ``OpenAI`` would work and open a pool nobody closes; the
    per-attempt client the run builds for itself is the one under test.
    """

    base_url = "http://eval.invalid/v1"
    api_key = "eval-key"


class TestRunResourceLifecycle:
    """A run must leave nothing behind.

    An iteration is ``len(cases) * n_runs`` of these and an optimizer
    loop is many iterations, so a leaked connection pool or a stranded
    workdir per run accumulates until a later run cannot open an fd —
    surfacing as a failed test attributed to the model under test rather
    than to the harness, which is a false score, not a crash.

    Each test drives the REAL ``_run_single_test`` with only the model
    lane stubbed, and asserts on the artifact (a closed transport, an
    absent directory, the process cwd) rather than on a recorded
    intention to close.
    """

    _CASE = {"user_prompt": "list the config files", "max_turns": 1}

    @staticmethod
    def _drive(
        monkeypatch,
        *,
        close_raises: type[BaseException] | None = None,
        session_ctor_raises: bool = False,
        init_raises: bool = False,
        teardown_reset_raises: bool = False,
        cwd_restore_raises: bool = False,
        loop_behavior: str | None = None,
        fast_retries: bool = False,
        test_timeout: int = 30,
    ):
        """One real run with the generation lane stubbed out.

        Returns the objects the run built, keyed for assertion.  The
        session subclass replaces ONLY ``_run_headless_loop`` (the sole
        step that would reach a model), so construction, the retry loop,
        the wall clock and the whole teardown path are production code.

        *loop_behavior*: ``"raise_always"`` models a persistent
        generation failure (every attempt raises); ``"hang"`` parks the
        worker on the session's own cancel event so the wall clock
        fires and the worker still exits promptly once cancelled.
        *fast_retries* neutralizes the retry backoff sleeps.
        """
        from turnstone.eval import core as core_module

        made: dict[str, Any] = {}

        if cwd_restore_raises:
            launch_cwd = os.getcwd()
            real_chdir = os.chdir
            budget = [1]

            def _chdir_failing_restore(path: str) -> None:
                # Models the launch directory going away mid-sweep (it is
                # removed, or its mount drops): the chdir INTO the workdir
                # still works, the restore does not.  One-shot, so the test
                # can put the process back afterwards.
                if os.path.abspath(path) == launch_cwd and budget:
                    budget.pop()
                    raise OSError(2, "No such file or directory", path)
                real_chdir(path)

            monkeypatch.setattr(os, "chdir", _chdir_failing_restore)

        if fast_retries:
            monkeypatch.setattr(time, "sleep", lambda _s: None)

        real_mkdtemp = tempfile.mkdtemp

        def _spy_mkdtemp(*a: Any, **kw: Any) -> str:
            path = real_mkdtemp(*a, **kw)
            if kw.get("prefix") == "turnstone_eval_":
                made["workdir"] = path
            return path

        real_openai = core_module.OpenAI

        def _spy_openai(**kw: Any) -> Any:
            made["run_client"] = client = real_openai(**kw)
            made.setdefault("run_clients", []).append(client)
            return client

        class _StubbedLaneSession(core_module.HeadlessSession):
            def __init__(self, **kw: Any) -> None:
                if session_ctor_raises:
                    # A raise between the per-attempt client and the
                    # attempt's ``try`` — the constructor here, or
                    # ``set_skill`` just after it.  Neither is covered by
                    # the in-loop closes.
                    raise RuntimeError("session construction failed")
                super().__init__(**kw)
                made["session"] = self
                made.setdefault("sessions", []).append(self)

            def _run_headless_loop(self, **kw: Any) -> list[dict[str, Any]]:
                made["cwd_in_run"] = os.getcwd()
                made["loop_calls"] = made.get("loop_calls", 0) + 1
                if loop_behavior == "hang":
                    self._cancelled.wait(10)
                    return []
                if loop_behavior == "raise_always":
                    raise RuntimeError("transient boom: connection reset by peer")
                return []

            def close(self) -> None:
                if close_raises is not None:
                    # Models a raise from one of ``ChatSession.close()``'s
                    # UNGUARDED steps, so nothing after it in the real
                    # close runs either.
                    raise close_raises("teardown blew up mid-close")
                super().close()

        monkeypatch.setattr(tempfile, "mkdtemp", _spy_mkdtemp)
        monkeypatch.setattr(core_module, "OpenAI", _spy_openai)
        monkeypatch.setattr(core_module, "HeadlessSession", _StubbedLaneSession)
        if init_raises:

            def _boom(*a: Any, **kw: Any) -> None:
                raise RuntimeError("storage init failed")

            monkeypatch.setattr(core_module, "init_storage", _boom)
        if teardown_reset_raises:
            real_reset = core_module.reset_storage
            resets: list[int] = []

            def _reset_then_fail(*a: Any, **kw: Any) -> None:
                # The run does its setup reset first; only the ``finally``
                # one fails, so the failure lands on a run that otherwise
                # completed.
                resets.append(1)
                if len(resets) > 1:
                    raise RuntimeError("storage reset failed")
                real_reset(*a, **kw)

            monkeypatch.setattr(core_module, "reset_storage", _reset_then_fail)

        try:
            made["result"] = core_module._run_single_test(
                _Params(),
                "eval-model",
                "you are a test",
                TestRunResourceLifecycle._CASE,
                0.7,
                1024,
                "medium",
                32768,
                test_timeout=test_timeout,
            )
        except BaseException as exc:  # noqa: BLE001 - the raise IS the fixture
            made["raised"] = exc
        return made

    def test_run_single_test_releases_its_session(self, monkeypatch):
        """The session was never closed on ANY path, so every run leaked
        its listener registrations, judge cancel events and background-shell
        registry.  Asserted on the registry itself — a spy counting
        ``close()`` calls would pass against a session that swallowed it."""
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch)

        assert "raised" not in made, made.get("raised")
        assert made["result"]["message_count"] == 1
        assert made["cwd_in_run"] == made["workdir"]  # it really did chdir in
        # The session's close ran to completion: the background-shell
        # registry is its last-but-one step and latches closed.
        assert made["session"]._background_shells._closed is True
        assert made["run_client"].is_closed()
        assert os.getcwd() == cwd_before
        assert not is_storage_initialized()
        assert not os.path.exists(made["workdir"])

    def test_a_raise_before_the_attempt_still_closes_the_client(self, monkeypatch):
        """Why the client close is kept even though the loop closes it on
        all three of ITS exits.  The per-attempt client is built before the
        attempt's ``try``, and so are the session constructor and
        ``set_skill``: a raise from either skipped every in-loop close and
        leaked the pool.  This is that window, and the only path on which
        the teardown's own close is the live one."""
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, session_ctor_raises=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "session" not in made  # it never finished constructing
        assert made["run_client"].is_closed(), "the per-attempt client leaked its pool"
        assert os.getcwd() == cwd_before
        assert not os.path.exists(made["workdir"])

    def test_init_storage_failure_still_removes_the_workdir(self, monkeypatch):
        """Storage setup lives INSIDE the ``try`` whose ``finally`` owns
        the directory.  Before that move a failing ``init_storage``
        stranded one temp dir per attempt, permanently."""
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, init_raises=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "workdir" in made, "the run never got as far as making one"
        assert not os.path.exists(made["workdir"])
        assert os.getcwd() == cwd_before

    def test_a_failing_storage_reset_still_removes_the_workdir(self, monkeypatch):
        """The teardown reset is nested, not suppressed.

        A storage fault is real and must surface, but it must not take the
        ``rmtree`` with it.  Flattened, this is the SETUP-path defect
        reappearing on the teardown path: one directory stranded
        permanently per run, i.e. the defect inside the fix for it.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, teardown_reset_raises=True)

        assert isinstance(made["raised"], RuntimeError)  # the fault surfaced
        assert "storage reset failed" in str(made["raised"])
        assert "result" not in made  # a finally-raise discards the return
        assert not os.path.exists(made["workdir"])  # and cost the run nothing
        assert os.getcwd() == cwd_before

        reset_storage()  # the patched reset never got to do its job

    def test_a_raising_session_close_still_completes_the_cleanup(self, monkeypatch):
        """A failing teardown costs the run NOTHING else.

        Each close is suppressed on its own, so a raise inside
        ``ChatSession.close()`` cannot take the close after it, the storage
        reset or the temp dir with it — and cannot reach the caller at all,
        since the run's result is already computed by then.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, close_raises=RuntimeError)

        assert "raised" not in made, made.get("raised")  # contained, not propagated
        assert made["result"]["message_count"] == 1  # and the measurement survived
        assert os.getcwd() == cwd_before
        assert not is_storage_initialized()
        assert not os.path.exists(made["workdir"])
        assert made["run_client"].is_closed()

    def test_a_ctrl_c_during_close_still_restores_the_cwd(self, monkeypatch):
        """What the cwd-restore-FIRST ordering is worth.

        A suppressed close cannot abort the block, so the ordering is not
        what saves an ordinary teardown failure.  It earns its keep on the
        two raises that DO leave the block early: the unsuppressed storage
        reset (above) and a ``BaseException``, which no suppression catches
        — realistically a Ctrl-C landing in ``_background_shells.close()``'s
        bounded join, the one blocking window a long run offers an impatient
        operator.  The residues are not equal: a leaked temp dir is inert
        and visible, while a process left chdir'd into a deleted directory
        silently breaks every subsequent run — which this test's own process
        would then demonstrate by dying at its next ``os.getcwd()``.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, close_raises=KeyboardInterrupt)

        assert isinstance(made["raised"], KeyboardInterrupt)
        assert made["cwd_in_run"] == made["workdir"]
        assert os.getcwd() == cwd_before  # survived the abort — it ran first
        # The honest cost of an abort mid-block, asserted rather than
        # implied: everything after the interrupt is skipped.
        assert os.path.exists(made["workdir"])
        assert is_storage_initialized()

        reset_storage()
        made["run_client"].close()
        shutil.rmtree(made["workdir"], ignore_errors=True)

    def test_a_failing_cwd_restore_still_completes_the_cleanup(self, monkeypatch):
        """The teardown's FIRST statement is guarded too.

        The restore is deliberately first, and the closes after it are
        suppressed on their own — but an unguarded ``os.chdir`` raise (the
        launch directory removed, or its mount dropped mid-run) would skip
        both closes, the storage reset AND the rmtree, reinstating the
        per-run leak in the one situation where the operator can least
        afford it.

        The failure is logged, not swallowed silently, and it is NOT
        re-raised: this run's result is already computed and is still
        honest.  The cost that remains is asserted below rather than
        implied — the process is left inside a directory the rmtree then
        removes, so the NEXT run fails early and visibly.
        """
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, cwd_restore_raises=True)
        os.chdir(cwd_before)  # first: the process is standing in a deleted dir

        assert "raised" not in made, made.get("raised")  # contained, not propagated
        assert made["result"]["message_count"] == 1  # and the measurement survived
        assert made["cwd_in_run"] == made["workdir"]  # the restore really was the failing call
        # Everything the unguarded chdir used to skip:
        assert made["session"]._background_shells._closed is True
        assert made["run_client"].is_closed(), "the per-attempt client leaked its pool"
        assert not is_storage_initialized()
        assert not os.path.exists(made["workdir"])

    def test_every_failed_attempts_session_is_closed(self, monkeypatch):
        """The generic-exception path CLOSES the session it replaces.

        The old shape dropped it with a comment citing the timeout
        path's reason — but on this path the future has already raised,
        the worker is done, and closing is safe.  Each of the three
        attempts abandons a fully-constructed session (background-shell
        registry, listener registrations) if the drop leaks here, and
        the sequential lane keeps them alive for the rest of the sweep.
        """
        made = self._drive(monkeypatch, loop_behavior="raise_always", fast_retries=True)

        assert isinstance(made["raised"], RuntimeError)
        assert "transient boom" in str(made["raised"])
        assert made["loop_calls"] == 3  # the retry loop really ran
        assert len(made["sessions"]) == 3
        assert all(s._background_shells._closed for s in made["sessions"])
        assert all(c.is_closed() for c in made["run_clients"])
        assert not os.path.exists(made["workdir"])
        assert not is_storage_initialized()

    def test_a_timed_out_run_drops_its_session_unclosed(self, monkeypatch):
        """The timeout path's drop is DELIBERATE and stays: the shutdown
        did not wait, so the worker is still inside the drive, and
        ``close()``'s bounded shell-join would trade a bounded leak for
        a blocked teardown on exactly the run already over budget.  This
        pins the divergence from the generic-exception path (the test
        above) from both sides, so neither can silently adopt the
        other's rule."""
        cwd_before = os.getcwd()
        made = self._drive(monkeypatch, loop_behavior="hang", test_timeout=1)

        assert isinstance(made["raised"], TimeoutError)
        assert made["loop_calls"] == 1  # a timeout aborts, never retries
        assert made["sessions"][0]._background_shells._closed is False  # dropped
        assert made["run_clients"][0].is_closed()  # the transport IS closed
        assert os.getcwd() == cwd_before
        assert not os.path.exists(made["workdir"])
        assert not is_storage_initialized()

        # Test hygiene, not production: reap the deliberately-dropped
        # session once its cancelled worker has exited.
        made["sessions"][0].close()
