"""Tests for turnstone.core.sandbox — validate_math_code and auto_print_wrap."""

from turnstone.core.sandbox import auto_print_wrap, validate_math_code


class TestValidateMathCode:
    def test_safe_code_no_errors(self):
        assert validate_math_code("x = 1 + 2\nprint(x)") == []

    def test_safe_math_import(self):
        assert validate_math_code("import math\nprint(math.pi)") == []

    def test_blocked_import_os(self):
        errors = validate_math_code("import os")
        assert len(errors) == 1
        assert "os" in errors[0]

    def test_blocked_import_sys(self):
        errors = validate_math_code("import sys")
        assert len(errors) == 1
        assert "sys" in errors[0]

    def test_blocked_import_subprocess(self):
        errors = validate_math_code("import subprocess")
        assert len(errors) == 1
        assert "subprocess" in errors[0]

    def test_blocked_from_import(self):
        errors = validate_math_code("from os.path import join")
        assert len(errors) == 1
        assert "os" in errors[0]

    def test_blocked_builtin_exec(self):
        errors = validate_math_code("exec('print(1)')")
        assert len(errors) == 1
        assert "exec" in errors[0]

    def test_blocked_builtin_eval(self):
        errors = validate_math_code("eval('1+1')")
        assert len(errors) == 1
        assert "eval" in errors[0]

    def test_blocked_builtin_open(self):
        errors = validate_math_code("open('file.txt')")
        assert len(errors) == 1
        assert "open" in errors[0]

    def test_blocked_dunder_access(self):
        errors = validate_math_code("x.__dict__")
        assert len(errors) == 1
        assert "__dict__" in errors[0]

    def test_allowed_dunder_name(self):
        # __name__, __doc__, __class__ are allowed
        assert validate_math_code("print(int.__name__)") == []

    def test_syntax_error_caught(self):
        errors = validate_math_code("def f(\n")
        assert len(errors) == 1
        assert "Syntax error" in errors[0]

    def test_multiple_violations(self):
        code = "import os\nimport sys\nexec('x')"
        errors = validate_math_code(code)
        assert len(errors) == 3


class TestAutoPrintWrap:
    def test_bare_expression_wrapped(self):
        result = auto_print_wrap("1 + 2")
        assert "print(" in result
        assert "1 + 2" in result

    def test_assignment_not_wrapped(self):
        code = "x = 1 + 2"
        assert auto_print_wrap(code) == code

    def test_code_with_print_not_wrapped(self):
        code = "x = 1\nprint(x)"
        assert auto_print_wrap(code) == code

    def test_code_with_result_assignment_not_wrapped(self):
        code = "result = 42"
        assert auto_print_wrap(code) == code

    def test_multiline_with_bare_expression_last(self):
        code = "x = 2\ny = 3\nx + y"
        result = auto_print_wrap(code)
        assert "print(" in result
        # The assignments should still be there
        assert "x = 2" in result
        assert "y = 3" in result

    def test_empty_code(self):
        assert auto_print_wrap("") == ""

    def test_syntax_error_returns_original(self):
        code = "def f(\n"
        assert auto_print_wrap(code) == code
