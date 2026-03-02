"""Tests for turnstone.core.safety — is_command_blocked and sanitize_command."""

from turnstone.core.safety import is_command_blocked, sanitize_command


class TestIsCommandBlocked:
    def test_rm_rf_root_blocked(self):
        result = is_command_blocked("rm -rf /")
        assert result is not None
        assert "Blocked" in result

    def test_rm_rf_star_blocked(self):
        result = is_command_blocked("rm -rf /*")
        assert result is not None

    def test_mkfs_blocked(self):
        result = is_command_blocked("mkfs /dev/sda")
        assert result is not None

    def test_shutdown_blocked(self):
        result = is_command_blocked("shutdown -h now")
        assert result is not None

    def test_fork_bomb_blocked(self):
        result = is_command_blocked(":(){ :|:& };:")
        assert result is not None

    def test_dd_if_blocked(self):
        result = is_command_blocked("dd if=/dev/zero of=/dev/sda")
        assert result is not None

    def test_safe_ls_returns_none(self):
        assert is_command_blocked("ls -la") is None

    def test_safe_git_returns_none(self):
        assert is_command_blocked("git status") is None

    def test_safe_python_returns_none(self):
        assert is_command_blocked("python script.py") is None

    def test_safe_rm_specific_file(self):
        assert is_command_blocked("rm file.txt") is None

    def test_whitespace_preserved(self):
        # Leading/trailing whitespace is stripped before checking
        result = is_command_blocked("  rm -rf /  ")
        assert result is not None


class TestSanitizeCommand:
    def test_left_single_curly_quote_replaced(self):
        assert sanitize_command("\u2018hello\u2019") == "'hello'"

    def test_double_curly_quotes_replaced(self):
        assert sanitize_command("\u201chello\u201d") == '"hello"'

    def test_en_dash_replaced(self):
        assert sanitize_command("ls \u2013la") == "ls -la"

    def test_em_dash_replaced(self):
        assert sanitize_command("cmd \u2014flag") == "cmd -flag"

    def test_plain_ascii_unchanged(self):
        cmd = "git commit -m 'test'"
        assert sanitize_command(cmd) == cmd

    def test_mixed_replacements(self):
        cmd = "\u201ctest\u201d \u2013flag"
        assert sanitize_command(cmd) == '"test" -flag'
