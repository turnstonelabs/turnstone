"""Capability-sensitive materialization of model-visible media.

This module owns the ordered PDF policy shared by stored attachments and
request-local tool content. Low-level PDF parsing and rendering remain in
``turnstone.core.pdf``; session state and model calls enter through the
injected perception callback.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from turnstone.core import pdf as pdf_ops
from turnstone.core.attachments import (
    neutralize_attachment_part,
    neutralize_untrusted_fences,
    safe_attachment_label,
)

if TYPE_CHECKING:
    from turnstone.core.providers._protocol import ModelCapabilities

PdfMode = Literal[
    "native",
    "rasterized",
    "perceived",
    "extracted_text",
    "resource_limited",
    "unreadable",
]
PdfContent = dict[str, Any] | list[dict[str, Any]]
PdfRasterizedParts = Callable[[], list[dict[str, Any]]]

PDF_RASTER_TRUNCATION_NOTICE = (
    f"[PDF rasterization stopped after {pdf_ops.PDF_RASTER_PAGE_CAP} pages; "
    "later pages were not provided.]"
)
_PDF_EXTRACTED_TEXT_NAME_SUFFIX = " (extracted text)"
_PDF_EXTRACTED_TEXT_MEDIA_TYPE = "text/plain"
_PDF_EXTRACTED_TEXT_NAME_MAX_CHARS = 200
# PDFium's truncation suffix and trust-marker neutralization can grow the raw
# prefix. Bound that final growth independently and reserve the same amount in
# the lane budget, so a hostile extracted string cannot invalidate planning.
PDF_EXTRACTED_TEXT_DATA_OVERHEAD_CHARS = 128
PDF_EXTRACTED_TEXT_PART_OVERHEAD_CHARS = (
    _PDF_EXTRACTED_TEXT_NAME_MAX_CHARS
    + len(_PDF_EXTRACTED_TEXT_NAME_SUFFIX)
    + len(_PDF_EXTRACTED_TEXT_MEDIA_TYPE)
    + PDF_EXTRACTED_TEXT_DATA_OVERHEAD_CHARS
)
_PDF_POSTPROCESS_TRUNCATION_NOTICE = (
    "\n\n... [PDF extracted text clipped after trust processing] ...\n"
)


@dataclass(frozen=True, slots=True)
class PdfSource:
    """Source-neutral PDF bytes and their request identity."""

    data: bytes
    filename: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class PdfMaterialization:
    """The wire-safe PDF representation selected for one target model."""

    content: PdfContent
    mode: PdfMode

    @property
    def readable(self) -> bool:
        return self.mode not in ("resource_limited", "unreadable")


PdfPerceiver = Callable[[PdfSource, PdfRasterizedParts], str | None]


def native_pdf_part(data: bytes, filename: str) -> dict[str, Any]:
    """Build the provider-neutral native PDF document part."""
    return {
        "type": "document",
        "document": {
            "name": filename,
            "media_type": "application/pdf",
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _png_data_uri(data: bytes) -> str:
    encoded = base64.b64encode(data).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _rasterized_pdf_parts(
    data: bytes,
    *,
    check_cancelled: Callable[[], None] | None,
) -> list[dict[str, Any]]:
    kwargs: dict[str, Any] = {}
    if check_cancelled is not None:
        kwargs["check_cancelled"] = check_cancelled
    rendered = pdf_ops.rasterize_pdf(data, **kwargs)
    parts = [
        {
            "type": "image_url",
            "image_url": {"url": _png_data_uri(page)},
        }
        for page in rendered
    ]
    if getattr(rendered, "truncated", False):
        parts.append({"type": "text", "text": PDF_RASTER_TRUNCATION_NOTICE})
    return parts


def _materialization(content: PdfContent, mode: PdfMode) -> PdfMaterialization:
    safe = cast("PdfContent", neutralize_attachment_part(content))
    return PdfMaterialization(content=safe, mode=mode)


def _bounded_extracted_text(text: str, prefix_cap: int) -> str:
    """Neutralize and cap the exact model-visible extracted representation."""
    safe = neutralize_untrusted_fences(text)
    final_cap = prefix_cap + PDF_EXTRACTED_TEXT_DATA_OVERHEAD_CHARS
    if len(safe) <= final_cap:
        return safe
    marker = _PDF_POSTPROCESS_TRUNCATION_NOTICE[:final_cap]
    return safe[: final_cap - len(marker)] + marker


def _extracted_or_unreadable(
    source: PdfSource,
    *,
    max_extracted_chars: int | None,
    check_cancelled: Callable[[], None] | None,
) -> PdfMaterialization:
    raw_name = source.filename or "document.pdf"
    name = neutralize_untrusted_fences(raw_name)[:_PDF_EXTRACTED_TEXT_NAME_MAX_CHARS]
    prefix_cap = (
        pdf_ops.PDF_TEXT_CHAR_CAP
        if max_extracted_chars is None
        else min(max(max_extracted_chars, 0), pdf_ops.PDF_TEXT_CHAR_CAP)
    )
    kwargs: dict[str, Any] = {}
    if max_extracted_chars is not None:
        kwargs["max_chars"] = max_extracted_chars
    if check_cancelled is not None:
        kwargs["check_cancelled"] = check_cancelled
    text = pdf_ops.extract_pdf_text(source.data, **kwargs)
    if not text:
        return _materialization(
            {
                "type": "text",
                "text": (
                    f"[PDF attachment '{safe_attachment_label(raw_name)}' — no extractable "
                    "text; this model cannot read PDFs natively]"
                ),
            },
            "unreadable",
        )
    return _materialization(
        {
            "type": "document",
            "document": {
                "name": f"{name}{_PDF_EXTRACTED_TEXT_NAME_SUFFIX}",
                "media_type": _PDF_EXTRACTED_TEXT_MEDIA_TYPE,
                "data": _bounded_extracted_text(text, prefix_cap),
            },
        },
        "extracted_text",
    )


def _resource_limited(source: PdfSource) -> PdfMaterialization:
    name = source.filename or "document.pdf"
    return _materialization(
        {
            "type": "text",
            "text": (
                f"[PDF attachment '{safe_attachment_label(name)}' — local processing "
                "exceeded safety limits; this model cannot read PDFs natively]"
            ),
        },
        "resource_limited",
    )


def materialize_pdf(
    source: PdfSource,
    capabilities: ModelCapabilities,
    *,
    perceive: PdfPerceiver,
    max_extracted_chars: int | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> PdfMaterialization:
    """Select one PDF representation for ``capabilities``.

    Precedence is native PDF, primary vision, configured perception, local text,
    then an explicit unreadable placeholder. The lazy rasterized-parts factory
    is memoized within this request so a perception cache hit renders nothing
    and no caller can render the same PDF twice during one materialization.

    The perception callback owns backend-error normalization: ``None`` means
    fall through. Exceptions, including cancellation, propagate unchanged.
    Every returned content part has passed attachment trust neutralization.
    """
    if capabilities.supports_pdf:
        return _materialization(native_pdf_part(source.data, source.filename), "native")

    try:
        rasterized: list[dict[str, Any]] | None = None

        def rasterized_parts() -> list[dict[str, Any]]:
            nonlocal rasterized
            if rasterized is None:
                rasterized = _rasterized_pdf_parts(
                    source.data,
                    check_cancelled=check_cancelled,
                )
            return rasterized

        if capabilities.supports_vision:
            pages = rasterized_parts()
            if pages:
                return _materialization(pages, "rasterized")
        else:
            perceived = perceive(source, rasterized_parts)
            if perceived:
                name = source.filename or "pdf"
                return _materialization(
                    {
                        "type": "text",
                        "text": (
                            f"[Perception of pdf attachment '{safe_attachment_label(name)}' "
                            f"(untrusted)]\n\n{perceived}"
                        ),
                    },
                    "perceived",
                )

        return _extracted_or_unreadable(
            source,
            max_extracted_chars=max_extracted_chars,
            check_cancelled=check_cancelled,
        )
    except pdf_ops.PdfWorkLimitError:
        # Resource exhaustion is terminal for this request. Retrying another
        # local PDFium operation would spend the same attacker-controlled work
        # twice and weaken the shared safety envelope.
        return _resource_limited(source)
