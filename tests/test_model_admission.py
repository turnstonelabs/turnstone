"""Focused contract tests for per-alias model admission."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

import turnstone.core.admission as admission_mod
from turnstone.core.admission import ModelAdmission
from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
from turnstone.core.model_registry import (
    KEY_GUARD_DEFERRED_TO_LIFESPAN,
    ModelConfig,
    ModelRegistry,
)


def _wait_until(predicate: Any, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.005)


def _config(alias: str, limit: int, *, base_url: str | None = None) -> ModelConfig:
    return ModelConfig(
        alias=alias,
        base_url=base_url or f"http://{alias}.example/v1",
        api_key="test",
        model="model",
        max_concurrency=limit,
    )


def test_unlimited_holders_are_counted_and_live_narrowing_drains() -> None:
    gate = ModelAdmission("primary", 0)
    first = gate.acquire()
    second = gate.acquire()
    assert gate.snapshot().in_flight == 2

    gate.set_limit(1)
    acquired = threading.Event()
    release_waiter = threading.Event()

    def _waiter() -> None:
        with gate.acquire():
            acquired.set()
            release_waiter.wait(2.0)

    thread = threading.Thread(target=_waiter, daemon=True)
    thread.start()
    _wait_until(lambda: gate.snapshot().queued == 1)

    first.release()
    assert not acquired.wait(0.05)
    second.release()
    assert acquired.wait(1.0)

    release_waiter.set()
    thread.join(1.0)
    assert not thread.is_alive()
    assert gate.snapshot().in_flight == 0


def test_fifo_waiters_and_hot_widening() -> None:
    gate = ModelAdmission("primary", 1)
    original = gate.acquire()
    acquired_order: list[int] = []
    acquired = [threading.Event(), threading.Event()]
    releases = [threading.Event(), threading.Event()]

    def _waiter(index: int) -> None:
        with gate.acquire():
            acquired_order.append(index)
            acquired[index].set()
            releases[index].wait(2.0)

    threads: list[threading.Thread] = []
    for index in range(2):
        thread = threading.Thread(target=_waiter, args=(index,), daemon=True)
        threads.append(thread)
        thread.start()
        _wait_until(lambda expected=index + 1: gate.snapshot().queued == expected)

    gate.set_limit(2)
    assert acquired[0].wait(1.0)
    assert not acquired[1].wait(0.05)

    original.release()
    assert acquired[1].wait(1.0)
    assert acquired_order == [0, 1]

    for event in releases:
        event.set()
    for thread in threads:
        thread.join(1.0)
        assert not thread.is_alive()


def test_cancelled_waiter_is_removed_and_never_admitted() -> None:
    gate = ModelAdmission("primary", 1)
    holder = gate.acquire()
    cancel_ref = StreamAbortRef()
    finished = threading.Event()
    errors: list[BaseException] = []

    def _waiter() -> None:
        try:
            gate.acquire(cancel_ref=cancel_ref)
        except BaseException as exc:  # test records the worker's exact exit
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=_waiter, daemon=True)
    thread.start()
    _wait_until(lambda: gate.snapshot().queued == 1)
    cancel_ref.abort()

    assert finished.wait(1.0)
    assert len(errors) == 1
    assert isinstance(errors[0], DeadlineCancelledError)
    assert gate.snapshot().queued == 0
    holder.release()
    assert gate.snapshot().in_flight == 0


def test_registry_keeps_one_gate_per_alias_across_resize_remove_and_readd() -> None:
    registry = ModelRegistry(
        {
            "alpha": _config("alpha", 1, base_url="http://shared.example/v1"),
            "beta": _config("beta", 3, base_url="http://shared.example/v1"),
        },
        default="alpha",
    )
    alpha = registry.get_admission("alpha")
    beta = registry.get_admission("beta")
    assert alpha is not beta

    registry.reload(
        {"alpha": _config("alpha", 2)},
        default="alpha",
        app_state=KEY_GUARD_DEFERRED_TO_LIFESPAN,
    )
    assert registry.get_admission("alpha") is alpha
    assert alpha.limit == 2

    registry.reload(
        {},
        default="",
        app_state=KEY_GUARD_DEFERRED_TO_LIFESPAN,
    )
    registry.reload(
        {"alpha": _config("alpha", 4)},
        default="alpha",
        app_state=KEY_GUARD_DEFERRED_TO_LIFESPAN,
    )
    assert registry.get_admission("alpha") is alpha
    assert alpha.limit == 4


def test_wait_stall_and_resize_logs_expose_queue_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(admission_mod, "_CANCEL_POLL_SECONDS", 0.005)
    monkeypatch.setattr(admission_mod, "_STALL_WARNING_SECONDS", 0.01)
    monkeypatch.setattr(
        admission_mod,
        "log",
        SimpleNamespace(
            info=lambda event, **fields: events.append((event, fields)),
            warning=lambda event, **fields: events.append((event, fields)),
        ),
    )
    gate = ModelAdmission("alpha", 1)
    holder = gate.acquire()

    thread = threading.Thread(target=lambda: gate.acquire().release(), daemon=True)
    thread.start()
    _wait_until(lambda: gate.snapshot().queued == 1)
    _wait_until(lambda: any(event == "model.admission_stalled" for event, _ in events))
    holder.release()
    thread.join(1.0)
    gate.set_limit(2)

    by_name = {event: fields for event, fields in events}
    assert by_name["model.admission_wait"]["alias"] == "alpha"
    assert by_name["model.admission_wait"]["queued_ahead"] == 0
    assert by_name["model.admission_stalled"]["in_flight"] == 1
    assert by_name["model.admission_stalled"]["queued"] == 1
    assert by_name["model.admission_resized"]["previous_limit"] == 1
    assert by_name["model.admission_resized"]["limit"] == 2
