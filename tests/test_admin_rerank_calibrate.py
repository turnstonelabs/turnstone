"""Tests for ``turnstone-admin rerank-calibrate`` (Phase 2 calibration CLI).

Drives the real ``_cmd_rerank_calibrate`` through a real sqlite ConfigStore.
``calibrate`` itself is stubbed (it needs a live endpoint); the rerank endpoint
is configured via ``tools.rerank_url`` so resolution returns a real client
object (no network until calibrate, which is stubbed). Mirrors the storage
setup in test_admin_export.py.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from turnstone.admin import _cmd_rerank_calibrate
from turnstone.core.config_store import ConfigStore
from turnstone.core.rerank_calibrate import CalibrationResult
from turnstone.core.storage import init_storage, reset_storage

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_storage_singleton() -> Iterator[None]:
    reset_storage()
    yield
    reset_storage()


def _args(db_path: str, *, apply: bool) -> argparse.Namespace:
    return argparse.Namespace(
        apply=apply,
        db_backend="sqlite",
        db_url="",
        db_path=db_path,
        db_pool_size=None,
        db_sslmode="",
        db_sslrootcert="",
        db_sslcert="",
        db_sslkey="",
    )


def _result(*, separated: bool, threshold: float | None) -> CalibrationResult:
    return CalibrationResult(
        model="m",
        raw_scale="probability (0-1)",
        separated=separated,
        suggested_threshold=threshold,
        relevant_min=0.8,
        relevant_max=0.9,
        irrelevant_min=0.1,
        irrelevant_max=0.2,
        n_relevant=7,
        n_irrelevant=42,
    )


def _seed_endpoint(db_path: str) -> None:
    ConfigStore(init_storage("sqlite", path=db_path)).set(
        "tools.rerank_url", "http://localhost:9999/rerank"
    )


def _stub_calibrate(monkeypatch, result: CalibrationResult) -> None:
    monkeypatch.setattr(
        "turnstone.core.rerank_calibrate.calibrate", lambda client, model="": result
    )


def _read_threshold(db_path: str) -> float:
    return ConfigStore(init_storage("sqlite", path=db_path)).get("tools.rerank_bm25_threshold")


def test_apply_writes_threshold(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    _seed_endpoint(db)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.37))
    _cmd_rerank_calibrate(_args(db, apply=True))
    assert _read_threshold(db) == 0.37
    assert "Applied" in capsys.readouterr().out


def test_no_apply_recommends_only(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    _seed_endpoint(db)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.37))
    _cmd_rerank_calibrate(_args(db, apply=False))
    assert _read_threshold(db) == 0.0  # not written
    out = capsys.readouterr().out
    assert "0.37" in out and "--apply" in out


def test_no_separation_does_not_apply(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    _seed_endpoint(db)
    _stub_calibrate(monkeypatch, _result(separated=False, threshold=None))
    with pytest.raises(SystemExit) as ei:
        _cmd_rerank_calibrate(_args(db, apply=True))
    assert ei.value.code == 1
    assert _read_threshold(db) == 0.0  # nothing written on no-separation


def test_no_endpoint_errors(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    init_storage("sqlite", path=db)  # schema only, no rerank_url configured
    for getter in ("get_rerank_url", "get_rerank_model", "get_rerank_api_key"):
        monkeypatch.setattr(f"turnstone.core.config.{getter}", lambda: "")
    with pytest.raises(SystemExit) as ei:
        _cmd_rerank_calibrate(_args(db, apply=False))
    assert ei.value.code == 1
