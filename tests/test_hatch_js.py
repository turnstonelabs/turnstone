"""Static smoke guards for the service-hatch container system.

``shared_static/hatch.{css,js}`` is the admin shelf/dialog chrome (the modal
redesign): a pane-scoped NON-modal shelf for create/edit/inspect and a
document-modal dialog tier for confirms/show-once.  Like the rest of the
WebUI there is no JS test framework, so the load-bearing invariants are
pinned as Python-side string-presence assertions.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_HATCH_JS = _ROOT / "turnstone/shared_static/hatch.js"
_HATCH_CSS = _ROOT / "turnstone/shared_static/hatch.css"
_CONSOLE_INDEX = _ROOT / "turnstone/console/static/index.html"
_UI_INDEX = _ROOT / "turnstone/ui/static/index.html"


def test_shelf_is_nonmodal_and_dialog_is_modal() -> None:
    """The TIERING invariant: the shelf opens with non-modal ``show()`` (the
    pane stays the containing block, other panes stay live — the split-pane
    contract) while the confirm tier opens with ``showModal()`` (top layer,
    stacks above any shelf)."""
    body = _HATCH_JS.read_text(encoding="utf-8")
    assert "dlg.show();" in body, "openShelf must use non-modal show()"
    assert "dlg.showModal();" in body, "openDialog must use showModal()"
    # The shelf path must NOT fall back to showModal — top layer cannot be
    # bound to a pane, which silently breaks the split-pane contract.
    shelf_fn = body.split("export function openShelf", 1)[1].split("export function", 1)[0]
    assert "showModal" not in shelf_fn


def test_shelf_focus_containment_is_inert_on_pane_siblings() -> None:
    """Non-modal means no free focus trap: containment comes from ``inert``
    on the host pane's OTHER children, restored on close."""
    body = _HATCH_JS.read_text(encoding="utf-8")
    assert "el.inert = true;" in body
    assert "el.inert = false;" in body
    # Pre-existing inertness must be respected, not clobbered on restore.
    assert "if (el.inert) continue;" in body


def test_shelf_escape_defers_to_a_modal_above() -> None:
    """Controller-owned Escape (non-modal dialogs have no native cancel)
    must NOT double-close when a document-modal confirm sits above the
    shelf — the native dialog owns that Escape."""
    body = _HATCH_JS.read_text(encoding="utf-8")
    assert 'document.querySelector("dialog:modal")' in body


def test_busy_lock_refuses_dismissal() -> None:
    """While a submit is in flight (data-busy) the container must hold:
    Escape, scrim clicks, [data-close], keyboard re-submit, and the dialog
    tier's native cancel are ALL refused."""
    body = _HATCH_JS.read_text(encoding="utf-8")
    assert 'top.hasAttribute("data-busy")' in body, "Escape must check busy"
    assert 'dlg.hasAttribute("data-busy")' in body, "data-close must check busy"
    # Scrim click mid-flight must not close the shelf.
    scrim_handler = body.split('scrim.addEventListener("click"', 1)[1].split("});", 1)[0]
    assert 'hasAttribute("data-busy")' in scrim_handler, (
        "the scrim click handler must hold the door while busy"
    )
    # Enter on the focused primary dispatches a click — a capture-phase guard
    # must swallow it before surface submit handlers re-fire the request.
    assert "{ capture: true }" in body, "busy needs the capture-phase guard"
    # The dialog tier's native Escape arrives as `cancel`.
    assert 'addEventListener("cancel"' in body, "openDialog must intercept cancel while busy"
    # Busy is announced, not just painted.
    assert 'setAttribute("aria-busy", "true")' in body


def test_window_bridge_for_classic_scripts() -> None:
    """admin.js/governance.js are classic scripts; they reach the ESM
    controller via the transitional window bridge (the toast.js pattern)."""
    body = _HATCH_JS.read_text(encoding="utf-8")
    assert "window.TurnstoneHatch = { openShelf, closeShelf, openDialog, setBusy };" in body


def test_console_loads_hatch_assets() -> None:
    html = _CONSOLE_INDEX.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/shared/hatch.css" />' in html
    assert '<script type="module" src="/shared/hatch.js"></script>' in html


def test_ui_loads_hatch_assets() -> None:
    html = _UI_INDEX.read_text(encoding="utf-8")
    assert '<link rel="stylesheet" href="/shared/hatch.css" />' in html
    assert '<script type="module" src="/shared/hatch.js"></script>' in html


def test_css_base_selector_is_class_only() -> None:
    """``dialog.hatch`` (0,1,1) would out-rank the container surface rules
    (0,1,0) and re-introduce the transparent-shelf bug — the base selector
    must stay class-only with containers winning on source order."""
    css = _HATCH_CSS.read_text(encoding="utf-8")
    assert re.search(r"^dialog\.hatch\b", css, flags=re.M) is None
    assert "\n.hatch {" in css
    assert css.index("\n.hatch {") < css.index(".hatch--shelf {"), (
        "containers must come AFTER the base rule to win on source order"
    )


def test_css_dialog_tier_restores_ua_centering() -> None:
    """The global reset flattens the UA's ``margin: auto`` that centers a
    modal dialog — the dialog tier must restore it."""
    css = _HATCH_CSS.read_text(encoding="utf-8")
    dialog_rule = css.split(".hatch--dialog {", 1)[1].split("}", 1)[0]
    assert "margin: auto;" in dialog_rule


def test_css_sheet_breakpoint_is_a_container_query() -> None:
    """A narrow SPLIT pane is narrow on a wide viewport: the bottom-sheet
    degradation keys off the PANE's width (@container), not the viewport."""
    css = _HATCH_CSS.read_text(encoding="utf-8")
    assert "container-type: inline-size;" in css
    assert "@container pane (max-width: 700px)" in css


def test_css_reduced_motion_and_light_theme_pass() -> None:
    css = _HATCH_CSS.read_text(encoding="utf-8")
    assert "@media (prefers-reduced-motion: reduce)" in css
    # The light-theme micro-text contrast pass (the .tab-menu-key precedent:
    # --ink-4 is sub-AA at 11px on light surfaces — one step up).
    assert '[data-theme="light"] .sh-foot-meta' in css


def test_hatch_markup_shape() -> None:
    """Every ``dialog.hatch`` in the console AND ui markup carries the full
    anatomy: a tier class, sh-head/sh-body/sh-foot, and aria-labelledby."""
    for index in (_CONSOLE_INDEX, _UI_INDEX):
        html = index.read_text(encoding="utf-8")
        for m in re.finditer(r"<dialog\b[^>]*class=\"[^\"]*\bhatch\b[^\"]*\"[^>]*>", html):
            tag = m.group(0)
            assert "hatch--shelf" in tag or "hatch--dialog" in tag, f"{index.name}: {tag}"
            assert 'aria-labelledby="' in tag, f"{index.name}: missing aria-labelledby: {tag}"
            # The dialog's body (up to its close tag) must have the three strips.
            rest = html[m.end() : html.index("</dialog>", m.end())]
            for cls in ("sh-head", "sh-body", "sh-foot"):
                assert cls in rest, f"{index.name}: dialog missing .{cls}: {tag}"


def test_classic_scripts_use_the_bridge_only_at_handler_time() -> None:
    """Module evaluation is deferred: a classic script touching
    ``TurnstoneHatch`` at parse time boots before the bridge exists (the
    #644 const-initializer lesson).  Heuristic guard: no top-level
    ``TurnstoneHatch`` use — every reference must sit inside a function
    body (indented)."""
    classic = [
        _ROOT / "turnstone/console/static" / name
        for name in ("admin.js", "governance.js", "app.js")
    ]
    classic.append(_ROOT / "turnstone/ui/static/app.js")
    for path in classic:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "TurnstoneHatch" in line and not line.startswith((" ", "\t")):
                raise AssertionError(
                    f"{path.name}:{i}: top-level TurnstoneHatch reference — "
                    "the window bridge only exists after modules evaluate"
                )
