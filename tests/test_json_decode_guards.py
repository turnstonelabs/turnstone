"""A JSON parse's decode-error handler also catches ``RecursionError``.

JSON nested too deeply for the parser raises ``RecursionError``, not ``ValueError``: Python 3.13 at
about 10,000 levels, 3.14 when the thread's C stack runs out. A handler that catches ``ValueError``
or ``JSONDecodeError`` around a parse lets such input past, so a request answers 500 where malformed
JSON gets a 400, and a stored value too deep to parse fails a read that handles an unparseable one.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

import turnstone

# What a handler must name to catch a decode error, and to catch RecursionError.
_DECODE_ERRORS = frozenset({"ValueError", "JSONDecodeError", "Exception", "BaseException"})
_RECURSION_ERRORS = frozenset({"RecursionError", "RuntimeError", "Exception", "BaseException"})


class _ParseGuards(ast.NodeVisitor):
    """Finds a module's JSON parses whose decode-error handler misses ``RecursionError``.

    A parse is ``json.loads`` or ``json.load``, under any name the module imports them as, or a
    ``.json()`` call with no arguments (an httpx2 response, a Starlette request). Its guard is the
    innermost ``try`` or ``contextlib.suppress`` block around it, in the same function, that
    catches a decode error or ``RecursionError``. The guard's first handler that catches a decode
    error must catch ``RecursionError`` too, unless one of its handlers names ``RecursionError``
    itself. A parse with no guard in its function raises to its caller, which this can't see: a
    helper whose callers guard it turns ``RecursionError`` into ``ValueError`` itself, as
    ``deserialize_value`` does. Exception tuples are resolved within the module only.
    """

    def __init__(self, tree: ast.Module) -> None:
        self._guards: list[list[ast.expr | None]] = []
        self._modules = {"json"}
        self._functions: set[str] = set()
        self._tuples: dict[str, ast.Tuple] = {}
        self.parses = 0
        self.misses: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self._modules.update(a.asname or a.name for a in node.names if a.name == "json")
            elif isinstance(node, ast.ImportFrom) and node.module == "json":
                self._functions.update(
                    a.asname or a.name for a in node.names if a.name in ("load", "loads")
                )
        for node in tree.body:
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._tuples[target.id] = node.value

    def _names(self, expr: ast.expr | None) -> set[str] | None:
        """The exception names *expr* catches; None for a bare ``except``."""
        if expr is None:
            return None
        if isinstance(expr, ast.Starred):
            return self._names(expr.value)
        if isinstance(expr, ast.Tuple):
            return set().union(*(self._names(elt) or set() for elt in expr.elts))
        if isinstance(expr, ast.Name):
            tuple_ = self._tuples.get(expr.id)
            return self._names(tuple_) if tuple_ is not None else {expr.id}
        if isinstance(expr, ast.Attribute):
            return {expr.attr}
        return set()

    def _guarded(self) -> bool:
        for handlers in reversed(self._guards):
            decode = recursion = None
            named = False
            for i, expr in enumerate(handlers):
                names = self._names(expr)
                if decode is None and (names is None or names & _DECODE_ERRORS):
                    decode = i
                if recursion is None and (names is None or names & _RECURSION_ERRORS):
                    recursion = i
                named = named or (names is not None and "RecursionError" in names)
            if decode is None and recursion is None:
                continue  # neither reaches this guard's handlers; look further out
            return decode is None or decode == recursion or named
        return True

    def _is_parse(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self._functions
        if not isinstance(func, ast.Attribute):
            return False
        if func.attr == "json":
            return not call.args and not call.keywords
        return (
            func.attr in ("load", "loads")
            and isinstance(func.value, ast.Name)
            and func.value.id in self._modules
        )

    def _new_scope(self, node: ast.AST) -> None:
        # A function defined inside a guard runs when it's called, not where it's defined.
        outer, self._guards = self._guards, []
        self.generic_visit(node)
        self._guards = outer

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._new_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._new_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._new_scope(node)

    def _try(self, node: ast.Try | ast.TryStar) -> None:
        self._guards.append([handler.type for handler in node.handlers])
        for stmt in node.body:
            self.visit(stmt)
        self._guards.pop()
        for part in (*node.handlers, *node.orelse, *node.finalbody):
            self.visit(part)

    def visit_Try(self, node: ast.Try) -> None:
        self._try(node)

    def visit_TryStar(self, node: ast.TryStar) -> None:
        self._try(node)

    def _with(self, node: ast.With | ast.AsyncWith) -> None:
        suppressed: list[ast.expr | None] = [
            ast.Tuple(elts=item.context_expr.args, ctx=ast.Load())
            for item in node.items
            if isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "attr", getattr(item.context_expr.func, "id", ""))
            == "suppress"
        ]
        for item in node.items:
            self.visit(item)
        if suppressed:
            self._guards.append(suppressed)
        for stmt in node.body:
            self.visit(stmt)
        if suppressed:
            self._guards.pop()

    def visit_With(self, node: ast.With) -> None:
        self._with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._with(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_parse(node):
            self.parses += 1
            if not self._guarded():
                self.misses.append(node.lineno)
        self.generic_visit(node)


def _misses(source: str) -> tuple[int, list[int]]:
    tree = ast.parse(textwrap.dedent(source))
    guards = _ParseGuards(tree)
    guards.visit(tree)
    return guards.parses, guards.misses


def test_every_json_parse_guard_catches_recursion_error() -> None:
    root = Path(turnstone.__file__).parent
    parses = 0
    misses: list[str] = []
    for path in sorted(root.rglob("*.py")):
        found, lines = _misses(path.read_text(encoding="utf-8"))
        parses += found
        misses += [f"{path.relative_to(root)}:{line}" for line in lines]
    assert parses > 0
    assert misses == [], (
        "add RecursionError to the handler that catches the parse's decode error: JSON nested too "
        "deeply to parse is malformed input"
    )


@pytest.mark.parametrize(
    "source",
    [
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            data = {}
        """,
        """
        async def handler(request):
            try:
                body = await request.json()
            except ValueError:
                return 400
        """,
        """
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(raw)
        """,
        # The decode error answers 400 and RecursionError falls through to a 500.
        """
        try:
            data = resp.json()
        except ValueError:
            return 400
        except Exception:
            return 500
        """,
        """
        import json as _json
        try:
            data = _json.loads(raw)
        except _json.JSONDecodeError:
            pass
        """,
        """
        from json import loads
        try:
            data = loads(raw)
        except ValueError:
            pass
        """,
        """
        _DECODE = (ValueError, TypeError)
        try:
            data = json.loads(raw)
        except _DECODE:
            pass
        """,
    ],
    ids=["try", "request-json", "suppress", "split", "module-alias", "function-alias", "tuple"],
)
def test_a_guard_that_misses_recursion_error_is_found(source: str) -> None:
    parses, misses = _misses(source)
    assert parses == 1
    assert len(misses) == 1


@pytest.mark.parametrize(
    "source",
    [
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, RecursionError):
            data = {}
        """,
        """
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        """,
        """
        with contextlib.suppress(ValueError, RecursionError):
            data = json.loads(raw)
        """,
        # A helper that turns RecursionError into the ValueError its callers catch.
        """
        try:
            return resp.json()
        except RecursionError as exc:
            raise ValueError("nested too deeply") from exc
        """,
        """
        try:
            data = json.loads(raw)
        except ValueError:
            return 400
        except RecursionError:
            return 413
        """,
        # No guard in its function: the caller sees what it raises.
        """
        def load(raw):
            return json.loads(raw)
        """,
        # The guard is around the definition, not the call.
        """
        try:
            def load(raw):
                return json.loads(raw)
        except ValueError:
            pass
        """,
        # A guard that catches neither error passes the parse's errors to the next one out.
        """
        try:
            try:
                data = json.loads(raw)
            except KeyError:
                pass
        except (ValueError, RecursionError):
            pass
        """,
    ],
    ids=[
        "named",
        "exception",
        "suppress",
        "translated",
        "separate-handler",
        "unguarded",
        "nested-def",
        "outer-guard",
    ],
)
def test_a_guard_that_catches_recursion_error_passes(source: str) -> None:
    parses, misses = _misses(source)
    assert parses == 1
    assert misses == []


# Request bodies nested too deeply to parse answer the 400 a malformed body gets.


@pytest.fixture
def console(sqlite_migrated_template, sqlite_backend_factory, tmp_path):
    """A console app on an empty database, and the database."""
    import shutil
    from unittest.mock import MagicMock

    from starlette.testclient import TestClient

    from turnstone.console.collector import ClusterCollector
    from turnstone.console.server import _load_static, create_app

    _load_static()
    path = tmp_path / "console.db"
    shutil.copyfile(sqlite_migrated_template, path)
    storage = sqlite_backend_factory(str(path), create_tables=False)
    app = create_app(
        collector=MagicMock(spec=ClusterCollector),
        jwt_secret="test-jwt-secret-minimum-32-chars!",
        auth_storage=storage,
    )
    # Without the app's startup and shutdown, which these routes don't need.
    client = TestClient(app, raise_server_exceptions=False)
    yield client, storage
    client.close()


_JSON = {"Content-Type": "application/json"}


def test_a_login_body_nested_too_deeply(console, monkeypatch) -> None:
    from tests._deep_json import too_deep_to_parse
    from turnstone.core.auth import hash_password

    client, storage = console
    storage.create_user("a" * 32, "ada", "Ada", hash_password("long-enough"))
    seed = '{"username": "ada", "password": "long-enough"}'
    too_deep_to_parse(monkeypatch, seed)
    resp = client.post("/v1/api/auth/login", content=seed, headers=_JSON)
    assert (resp.status_code, resp.json()) == (400, {"error": "Invalid JSON body"})


def test_a_setup_body_nested_too_deeply(console, monkeypatch) -> None:
    from tests._deep_json import too_deep_to_parse

    client, storage = console
    seed = '{"username": "ada", "display_name": "Ada", "password": "long-enough"}'
    too_deep_to_parse(monkeypatch, seed)
    resp = client.post("/v1/api/auth/setup", content=seed, headers=_JSON)
    assert (resp.status_code, resp.json()) == (400, {"error": "Invalid JSON body"})
    assert storage.count_users() == 0


def test_a_notify_body_nested_too_deeply(sqlite_backend_factory, tmp_path, monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from starlette.testclient import TestClient

    from tests._deep_json import too_deep_to_parse
    from tests.test_notify_http import _JWT_SECRET, _auth_headers
    from turnstone.channels._http import create_channel_app

    adapter = AsyncMock()
    adapter.channel_type = "discord"
    storage = sqlite_backend_factory(str(tmp_path / "channels.db"))
    seed = '{"target": {"channel_type": "discord", "channel_id": "123456"}, "message": "Hello!"}'
    too_deep_to_parse(monkeypatch, seed)
    app = create_channel_app({"discord": adapter}, storage, jwt_secret=_JWT_SECRET)
    with TestClient(app) as client:
        resp = client.post("/v1/api/notify", content=seed, headers={**_auth_headers(), **_JSON})
    assert (resp.status_code, resp.json()) == (400, {"error": "Invalid JSON body"})
    adapter.send.assert_not_called()
