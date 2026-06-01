"""Tests for ``turnstone-admin rerank-calibrate`` (Phase 3 per-model calibration).

Drives the real ``_cmd_rerank_calibrate`` through a real sqlite storage. The CLI
is now per-model: it requires ``--model <alias>``, resolves that reranker model
definition, calibrates its endpoint, and (with ``--apply``) writes the three
calibration fields onto the model's capabilities — NOT the global
``tools.rerank_bm25_threshold``. ``calibrate_model`` is stubbed (it needs a live
endpoint). Mirrors the storage setup in test_admin_export.py.
"""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING

import pytest

from turnstone.admin import _cmd_rerank_calibrate
from turnstone.core.rerank_calibrate import CalibrationResult
from turnstone.core.storage import init_storage, reset_storage

if TYPE_CHECKING:
    from collections.abc import Iterator

_ALIAS = "my-reranker"


@pytest.fixture(autouse=True)
def _reset_storage_singleton() -> Iterator[None]:
    reset_storage()
    yield
    reset_storage()


def _args(db_path: str, *, apply: bool, model: str = _ALIAS) -> argparse.Namespace:
    return argparse.Namespace(
        model=model,
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


def _seed_reranker(db_path: str, *, base_url: str = "http://localhost:9999/rerank") -> str:
    """Create a reranker model definition; return its db_path-backed alias."""
    storage = init_storage("sqlite", path=db_path)
    storage.create_model_definition(
        definition_id="def-reranker",
        alias=_ALIAS,
        model="bge-reranker",
        provider="openai-compatible",
        base_url=base_url,
        api_key="",
        context_window=0,
        capabilities=json.dumps({"supports_rerank": True}),
    )
    return _ALIAS


def _stub_calibrate(monkeypatch, result: CalibrationResult) -> None:
    monkeypatch.setattr(
        "turnstone.core.rerank_calibrate.calibrate_model",
        lambda base_url, model, api_key, *, instruction="", timeout=60.0: result,
    )


def _read_caps(db_path: str) -> dict:
    storage = init_storage("sqlite", path=db_path)
    row = storage.get_model_definition_by_alias(_ALIAS)
    assert row is not None
    return json.loads(row["capabilities"] or "{}")


def test_apply_writes_model_caps(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    _seed_reranker(db)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.37))
    _cmd_rerank_calibrate(_args(db, apply=True))
    caps = _read_caps(db)
    assert caps["rerank_threshold"] == 0.37
    assert caps["rerank_scale"] == "probability (0-1)"
    assert caps["rerank_separated"] is True
    # The original capability is preserved (merge, not replace).
    assert caps["supports_rerank"] is True
    assert "Applied" in capsys.readouterr().out


def test_no_apply_recommends_only(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "t.db")
    _seed_reranker(db)
    _stub_calibrate(monkeypatch, _result(separated=True, threshold=0.37))
    _cmd_rerank_calibrate(_args(db, apply=False))
    assert "rerank_threshold" not in _read_caps(db)  # not written
    out = capsys.readouterr().out
    assert "0.37" in out and "--apply" in out


def test_no_separation_persists_marker_on_apply(tmp_path, monkeypatch, capsys):
    """A no-separation result is a valid recorded outcome: with --apply it still
    persists the marker (rerank_scale + separated=False, threshold 0.0) — aligned
    with the calibrate endpoint — and exits 0 (no floor)."""
    db = str(tmp_path / "t.db")
    _seed_reranker(db)
    _stub_calibrate(monkeypatch, _result(separated=False, threshold=None))
    _cmd_rerank_calibrate(_args(db, apply=True))  # no SystemExit
    caps = _read_caps(db)
    assert caps["rerank_scale"] == "probability (0-1)"
    assert caps["rerank_separated"] is False
    assert caps["rerank_threshold"] == 0.0
    assert caps["supports_rerank"] is True  # merge, not replace
    assert "No clean separation" in capsys.readouterr().out


def test_unknown_model_errors(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    init_storage("sqlite", path=db)  # schema only, no model definition
    with pytest.raises(SystemExit) as ei:
        _cmd_rerank_calibrate(_args(db, apply=False, model="nope"))
    assert ei.value.code == 1


def test_model_without_base_url_errors(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    _seed_reranker(db, base_url="")
    with pytest.raises(SystemExit) as ei:
        _cmd_rerank_calibrate(_args(db, apply=False))
    assert ei.value.code == 1
