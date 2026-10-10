"""The livepass console harness carries what the console page gives admin.js.

The harness embeds the admin fragment and lists its own script tags, so a dependency the console
page adds outside the fragment (a classic script ahead of admin.js, a ``<template>``) goes missing
from the harness unnoticed: its ``?open=`` drivers only fail when someone runs them in a browser.
The schedule shelf's drivers threw that way, without ``schedule_builder.js`` and its template.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts/livepass.py"
_INDEX = _ROOT / "turnstone/console/static/index.html"


@pytest.fixture(scope="module")
def console_page(tmp_path_factory: pytest.TempPathFactory) -> str:
    spec = importlib.util.spec_from_file_location("livepass_console_script", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path_factory.mktemp("livepass")
    module.build(out)
    return (out / "console/livepass.html").read_text(encoding="utf-8")


def test_console_harness_loads_the_classic_scripts_admin_js_follows(console_page: str) -> None:
    classic = re.findall(
        r'<script src="/static/([\w.-]+\.js)"></script>', _INDEX.read_text(encoding="utf-8")
    )
    needed = classic[: classic.index("admin.js") + 1]
    assert "schedule_builder.js" in needed
    loaded = re.findall(r'<script src="console-static/([\w.-]+\.js)"></script>', console_page)
    missing = [name for name in needed if name not in loaded]
    assert not missing, f"the harness must load {missing} as the console page does"
    order = [loaded.index(name) for name in needed]
    assert order == sorted(order), "the harness must load them in the console page's order"


def test_console_harness_carries_every_template(console_page: str) -> None:
    ids = re.findall(r'<template id="([^"]+)"', _INDEX.read_text(encoding="utf-8"))
    assert "schedule-when-template" in ids
    for template_id in ids:
        assert f'<template id="{template_id}"' in console_page, template_id
