"""Sandboxed Python executor for the math tool."""

from __future__ import annotations

import ast
import multiprocessing
import re
import traceback
from typing import Any

_MATH_BLOCKED_BUILTINS = {
    "open",
    "exec",
    "eval",
    "compile",
    "input",
    "breakpoint",
    "memoryview",
    "globals",
    "locals",
    "vars",
    # Reflection primitives — bypass AST dunder checks via runtime strings
    "getattr",
    "setattr",
    "delattr",
    # Type system — can reconstruct arbitrary classes
    "type",
    # Import — the replaced _safe_import is in the namespace, but block the
    # name so direct __import__ calls are caught by the AST validator
    "__import__",
}

_MATH_BLOCKED_MODULES = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "pathlib",
    "socket",
    "http",
    "urllib",
    "requests",
    "pickle",
    "marshal",
    "shelve",
    "dbm",
    "sqlite3",
    "ctypes",
    "multiprocessing",
    "threading",
    "asyncio",
    "concurrent",
    "signal",
    "pty",
    "tty",
    "termios",
    "fcntl",
    "resource",
    "syslog",
    "tempfile",
    "io",
    "builtins",
    "__builtin__",
    "importlib",
}


class _ASTValidator(ast.NodeVisitor):
    """Validates AST for dangerous constructs."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".")[0] in _MATH_BLOCKED_MODULES:
                self.errors.append(f"Import of '{alias.name}' is not allowed")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.module.split(".")[0] in _MATH_BLOCKED_MODULES:
            self.errors.append(f"Import from '{node.module}' is not allowed")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in _MATH_BLOCKED_BUILTINS:
            self.errors.append(f"Call to '{node.func.id}' is not allowed")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr.startswith("__")
            and node.attr.endswith("__")
            and node.attr not in {"__name__", "__doc__", "__class__"}
        ):
            self.errors.append(f"Access to '{node.attr}' is not allowed")
        # Block operator.attrgetter/itemgetter which act as getattr bypasses
        if node.attr in ("attrgetter", "itemgetter"):
            self.errors.append(f"Access to '{node.attr}' is not allowed")
        self.generic_visit(node)


def validate_math_code(code: str) -> list[str]:
    """Validate code for dangerous constructs. Returns list of errors."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        lines = code.split("\n")
        msg = f"Syntax error on line {e.lineno}: {e.msg}"
        if e.lineno and e.lineno <= len(lines):
            msg += f"\n  {e.lineno}: {lines[e.lineno - 1]}"
            if e.offset:
                msg += f"\n      {' ' * (e.offset - 1)}^"
        return [msg]
    except (ValueError, UnicodeError) as e:
        return [f"Code contains invalid characters: {e}"]
    v = _ASTValidator()
    v.visit(tree)
    return v.errors


def _math_exec_in_process(code: str, result_queue: multiprocessing.Queue[tuple[str, str]]) -> None:
    """Execute code in a subprocess, put (status, output) in queue."""
    import contextlib
    import signal as _signal
    import sys as _sys
    from io import StringIO

    _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)
    _signal.signal(_signal.SIGINT, _signal.SIG_DFL)
    _sys.set_int_max_str_digits(100_000)

    try:
        captured = StringIO()
        _sys.stdout = captured

        def _safe_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.split(".")[0] in _MATH_BLOCKED_MODULES:
                raise ImportError(f"Import of '{name}' is blocked")
            mod = original_import(name, *args, **kwargs)
            # Strip __builtins__ from every imported module so
            # module.__builtins__['__import__'] can't bypass _safe_import
            # (covers operator.attrgetter('__builtins__') and similar).
            if hasattr(mod, "__builtins__"):
                with contextlib.suppress(AttributeError, TypeError):
                    mod.__builtins__ = {}  # type: ignore[attr-defined]
            return mod

        original_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )
        safe_builtins = (
            {k: v for k, v in __builtins__.items() if k not in _MATH_BLOCKED_BUILTINS}
            if isinstance(__builtins__, dict)
            else {
                k: getattr(__builtins__, k)
                for k in dir(__builtins__)
                if k not in _MATH_BLOCKED_BUILTINS and not k.startswith("_")
            }
        )
        safe_builtins["__import__"] = _safe_import

        # Pre-import safe modules
        import collections
        import decimal
        import fractions
        import functools
        import itertools
        import math
        import operator
        import random
        import re
        import string

        ns: dict[str, Any] = {
            "__builtins__": safe_builtins,
            "math": math,
            "fractions": fractions,
            "Fraction": fractions.Fraction,
            "itertools": itertools,
            "functools": functools,
            "operator": operator,
            "collections": collections,
            "decimal": decimal,
            "Decimal": decimal.Decimal,
            "random": random,
            "re": re,
            "string": string,
        }

        try:
            import sympy

            ns["sympy"] = sympy
            for name in (
                "symbols",
                "Symbol",
                "solve",
                "simplify",
                "expand",
                "factor",
                "Eq",
                "sqrt",
                "Rational",
                "pi",
                "E",
                "I",
                "oo",
                "sin",
                "cos",
                "tan",
                "exp",
                "log",
                "factorial",
                "binomial",
                "gcd",
                "lcm",
                "prime",
                "isprime",
                "factorint",
                "divisors",
                "totient",
                "mod_inverse",
                "Matrix",
                "integrate",
                "diff",
                "limit",
                "series",
                "Sum",
                "Product",
                "floor",
                "ceiling",
                "Abs",
            ):
                ns[name] = getattr(sympy, name)
        except ImportError:
            pass

        try:
            import numpy as _np

            ns["np"] = ns["numpy"] = _np
        except ImportError:
            pass

        try:
            import scipy  # type: ignore[import-untyped]
            import scipy.integrate  # type: ignore[import-untyped]
            import scipy.linalg  # type: ignore[import-untyped]
            import scipy.optimize  # type: ignore[import-untyped]
            import scipy.special  # type: ignore[import-untyped]

            ns["scipy"] = scipy
            ns["special"] = scipy.special
            ns["optimize"] = scipy.optimize
            ns["comb"] = scipy.special.comb
            ns["perm"] = scipy.special.perm
            ns["gamma"] = scipy.special.gamma
            ns["beta"] = scipy.special.beta
        except ImportError:
            pass

        # Strip __builtins__ from all pre-imported modules so
        # module.__builtins__['__import__'] can't bypass _safe_import.
        for v in list(ns.values()):
            if hasattr(v, "__builtins__"):
                with contextlib.suppress(AttributeError, TypeError):
                    v.__builtins__ = {}

        exec(code, ns)  # noqa: S102

        _sys.stdout = _sys.__stdout__
        printed = captured.getvalue()
        result_var = ns.get("result")
        if result_var is not None:
            out = f"{printed.rstrip()}\nresult = {result_var}" if printed else str(result_var)
        elif printed:
            out = printed.rstrip()
        else:
            out = "No output. Add print() to see results."
        result_queue.put(("success", out))

    except Exception as e:
        _sys.stdout = _sys.__stdout__
        result_queue.put(("error", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))


def auto_print_wrap(code: str) -> str:
    """If code has no print/result and the last statement is an expression, wrap it in print()."""
    # Skip if code already has print() or assigns to 'result'
    if "print(" in code or re.search(r"\bresult\s*=", code):
        return code
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code
    if not tree.body:
        return code
    last = tree.body[-1]
    if isinstance(last, ast.Expr):
        # Get the source of the last expression and wrap in print()
        lines = code.split("\n")
        last_line_start = last.lineno - 1  # 0-based
        last_line_end = last.end_lineno  # 1-based, exclusive after slicing
        expr_lines = lines[last_line_start:last_line_end]
        expr_text = "\n".join(expr_lines)
        prefix = lines[:last_line_start]
        wrapped = prefix + [f"print({expr_text})"]
        return "\n".join(wrapped)
    return code


def execute_math_sandboxed(code: str, timeout: float = 30.0) -> tuple[str, bool]:
    """Execute Python code in a sandboxed subprocess. Returns (output, is_error)."""
    code = auto_print_wrap(code)
    errors = validate_math_code(code)
    if errors:
        return "Validation errors:\n" + "\n".join(f"- {e}" for e in errors), True

    result_queue: multiprocessing.Queue[tuple[str, str]] = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_math_exec_in_process, args=(code, result_queue))
    proc.start()
    proc.join(timeout=timeout)

    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=1.0)
        if proc.is_alive():
            proc.kill()
            proc.join()
        result_queue.close()
        result_queue.join_thread()
        return f"Execution timed out after {timeout}s", True

    if result_queue.empty():
        result_queue.close()
        result_queue.join_thread()
        return "Execution failed with no output", True

    status, output = result_queue.get()
    result_queue.close()
    result_queue.join_thread()
    return output, status == "error"
