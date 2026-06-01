"""Reranker threshold calibration (Phase 2 core).

Probes a configured rerank endpoint with a small bundled set of labelled
relevant/irrelevant query groups and recommends a ``tools.rerank_bm25_threshold``
floor that separates the two — or reports that the endpoint cannot cleanly
separate them (which doubles as a mis-served-reranker health check).

Scores are normalised to a 0-1 relevance probability via
``rerank.normalize_scores`` (sigmoid for logit endpoints) — the SAME transform
the ``_bm25_reranker`` closure applies — so the recommended floor is in the
exact space the production comparison uses. The raw score scale is detected and
reported separately for diagnostics. ``scripts/bench_bm25_rerank.py`` is the
manual prototype of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from turnstone.core.rerank import normalize_scores

if TYPE_CHECKING:
    from turnstone.core.rerank import RerankClient

# Labelled probe groups: each doc is clearly relevant to its own query and
# irrelevant to every other query (distinct topics, with a few technical ones to
# approximate the tool/skill/memory domain). For each query we send its own doc
# plus all the others; the own-doc score is "relevant", the rest "irrelevant".
_PROBE_SET: list[tuple[str, str]] = [
    ("capital of France", "Paris is the capital and largest city of France."),
    ("sort a list in Python", "Use sorted() or the list.sort() method to order a Python list."),
    ("symptoms of dehydration", "Dehydration causes thirst, dry mouth, fatigue, and dark urine."),
    (
        "how photosynthesis works",
        "Photosynthesis turns sunlight, water, and CO2 into glucose and oxygen in chloroplasts.",
    ),
    (
        "rotate a JWT signing secret",
        "Rotate a JWT secret by signing new tokens with a new key while still accepting the old "
        "key until previously issued tokens expire.",
    ),
    (
        "Dockerfile layer cache best practice",
        "Order Dockerfile instructions from least- to most-frequently changing to maximise build "
        "cache reuse.",
    ),
    ("treat a sprained ankle", "For a sprained ankle use RICE: rest, ice, compression, elevation."),
    (
        "boiling point of water at sea level",
        "Water boils at 100 degrees Celsius (212 Fahrenheit) at sea-level pressure.",
    ),
    (
        "what causes ocean tides",
        "Ocean tides are caused mainly by the Moon's gravitational pull on Earth's oceans.",
    ),
    (
        "reverse a string in JavaScript",
        "Reverse a JavaScript string with str.split('').reverse().join('').",
    ),
    (
        "difference between TCP and UDP",
        "TCP is connection-oriented and reliable; UDP is connectionless and best-effort.",
    ),
    ("who painted the Mona Lisa", "The Mona Lisa was painted by Leonardo da Vinci."),
    (
        "set up SSH key-based login",
        "Append your public key to ~/.ssh/authorized_keys to enable key-based SSH login.",
    ),
    (
        "what is compound interest",
        "Compound interest is interest on the principal plus all previously accrued interest.",
    ),
    ("largest planet in the solar system", "Jupiter is the largest planet in the solar system."),
    (
        "prevent SQL injection",
        "Prevent SQL injection by using parameterised queries or prepared statements rather than "
        "string concatenation.",
    ),
    (
        "how to brew espresso",
        "Espresso is made by forcing hot water at high pressure through finely-ground coffee.",
    ),
    (
        "convert Celsius to Fahrenheit",
        "To convert Celsius to Fahrenheit, multiply by 9/5 and add 32.",
    ),
]

# How far into the relevant/irrelevant gap to place the floor, measured from the
# irrelevant (high) edge. 0.25 biases toward RECALL: the bundled probes are
# generic, so the real tool/skill/memory domain may separate slightly
# differently — under-drop rather than over-drop.
_GAP_FRACTION = 0.25


@dataclass(frozen=True)
class CalibrationResult:
    """Outcome of probing a rerank endpoint with the labelled set.

    Score fields are in the NORMALISED 0-1 space (the space the floor compares
    in); ``raw_scale`` describes the endpoint's untransformed output.
    """

    model: str
    raw_scale: str  # "probability (0-1)" | "logit (sigmoid-normalised)" | "unknown (no scores)"
    separated: bool
    suggested_threshold: float | None  # 0-1, None when the classes overlap
    relevant_min: float
    relevant_max: float
    irrelevant_min: float
    irrelevant_max: float
    n_relevant: int
    n_irrelevant: int


def _warmup(client: RerankClient, rounds: int = 3) -> None:
    """Prime a cold rerank endpoint before measuring.

    A freshly-booted vLLM endpoint compiles / captures CUDA graphs on the first
    request of a new batch shape, which can exceed the per-call timeout. Send
    throwaway calls (of the probe batch shape) until one succeeds, so the real
    loop below doesn't time out on the cold start. Best-effort — give up after
    ``rounds`` and let calibration surface any persistent failure.
    """
    docs = [doc for _, doc in _PROBE_SET]
    for _ in range(rounds):
        try:
            client.rerank("warmup", docs)
            return
        except Exception:
            continue


def calibrate(client: RerankClient, *, model: str = "") -> CalibrationResult:
    """Probe ``client`` with the labelled set and recommend a 0-1 floor.

    For each query, send its own doc plus every other probe doc (no ``top_n`` so
    all are scored), normalise each response to 0-1, pool the own-doc scores as
    "relevant" and the rest as "irrelevant", then place a recall-biased floor in
    the gap — or report ``separated=False`` (no recommendation) when the pooled
    classes overlap.
    """
    _warmup(client)  # absorb cold-start compile latency before measuring
    docs_all = [doc for _, doc in _PROBE_SET]
    relevant: list[float] = []
    irrelevant: list[float] = []
    raw_all: list[float] = []
    for i, (query, _) in enumerate(_PROBE_SET):
        docs = [docs_all[i]] + [d for j, d in enumerate(docs_all) if j != i]
        hits = client.rerank(query, docs)
        raw_all.extend(h.score for h in hits)
        norm = normalize_scores([h.score for h in hits])
        by_index = {h.index: s for h, s in zip(hits, norm, strict=True)}
        if 0 in by_index:
            relevant.append(by_index[0])
        irrelevant.extend(by_index[idx] for idx in range(1, len(docs)) if idx in by_index)
    return _build_result(model, _raw_scale(raw_all), relevant, irrelevant)


def _raw_scale(raw: list[float]) -> str:
    if not raw:
        return "unknown (no scores)"
    if all(0.0 <= s <= 1.0 for s in raw):
        return "probability (0-1)"
    return "logit (sigmoid-normalised)"


def _build_result(
    model: str, raw_scale: str, relevant: list[float], irrelevant: list[float]
) -> CalibrationResult:
    """Pool the labelled (normalised) scores, detect separation, place a floor.

    Separation is judged on the POOLED classes (lowest relevant vs highest
    irrelevant across all queries) because the production floor is a single
    global value — if the pooled classes overlap, no single threshold cleanly
    works, which is exactly the signal an operator needs.
    """
    if not relevant or not irrelevant:
        return CalibrationResult(
            model, raw_scale, False, None, 0.0, 0.0, 0.0, 0.0, len(relevant), len(irrelevant)
        )
    r_min, r_max = min(relevant), max(relevant)
    i_min, i_max = min(irrelevant), max(irrelevant)
    separated = r_min > i_max
    suggested = round(i_max + _GAP_FRACTION * (r_min - i_max), 4) if separated else None
    return CalibrationResult(
        model,
        raw_scale,
        separated,
        suggested,
        r_min,
        r_max,
        i_min,
        i_max,
        len(relevant),
        len(irrelevant),
    )
