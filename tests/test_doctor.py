"""Tests for the turnstone-doctor diagnostic tool."""

from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from turnstone import __version__
from turnstone.doctor import (
    SYSTEM_PROMPT,
    TOOLS,
    BackendVerdict,
    ConfigFileInfo,
    InstallProfile,
    VersionReport,
    _compose_image_tag,
    _derive_health_urls,
    _discover_config_files,
    _DoctorLLM,
    _fetch_upstream_versions,
    _find_compose_files,
    _find_repo_root,
    _FinishError,
    _http_get_json,
    _mask_secrets,
    _parse_config_sections,
    _primary_kind,
    _read_api_creds,
    _redact_url_credentials,
    _relevant_env,
    _resolve_db_config,
    _run_conversation,
    _scrub_tool_output,
    _select_provider,
    _tool_check_docker,
    _tool_check_port,
    _tool_compose_logs,
    _tool_compose_status,
    _tool_finish,
    _tool_journal_tail,
    _tool_node_health,
    _tool_read_file,
    _tool_systemd_status,
    _version_behind,
    check_versions,
    detect_install_profile,
    execute_tool,
    family_of,
    open_storage,
    render_full_report,
    render_profile_report,
    render_version_report,
    resolve_doctor_brain,
)

MUTATING_TOKENS = frozenset(
    {"up", "down", "restart", "stop", "start", "rm", "exec", "create", "delete", "kill", "build"}
)


def _make_profile(tmp_path: Path, **overrides: object) -> InstallProfile:
    """Build an InstallProfile with sane defaults for tests."""
    base: dict[str, object] = {
        "project_dir": tmp_path,
        "kinds": [],
        "primary_kind": "unknown",
        "install_source": "unknown",
        "repo_root": None,
        "docker_available": False,
        "compose_files": [],
        "compose_ps": "",
        "systemd_units": [],
        "config_files": [],
        "env_present": {},
        "db_config": {"backend": "sqlite", "url": "", "path": ""},
        "health_urls": [],
    }
    base.update(overrides)
    return InstallProfile(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------


class TestMaskSecrets:
    def test_masks_env_api_key(self) -> None:
        out = _mask_secrets("OPENAI_API_KEY=sk-1234567890abcdef")
        assert "sk-1" in out and "cdef" in out
        assert "1234567890abcde" not in out

    def test_masks_toml_api_key(self) -> None:
        out = _mask_secrets('api_key = "sk-1234567890abcdef"')
        assert "sk-1234567890abcdef" not in out
        assert "****" in out

    def test_masks_toml_jwt_secret(self) -> None:
        out = _mask_secrets('jwt_secret = "deadbeefdeadbeefdeadbeef"')
        assert "deadbeefdeadbeefdeadbeef" not in out

    def test_redacts_db_url_password(self) -> None:
        line = 'url = "postgresql+psycopg://turnstone:supersecret@db:5432/turnstone"'
        out = _mask_secrets(line)
        assert "supersecret" not in out
        assert "turnstone:****@db" in out
        # host and db name stay visible for diagnosis
        assert "db:5432/turnstone" in out

    def test_base_url_not_masked(self) -> None:
        out = _mask_secrets("LLM_BASE_URL=http://localhost:8000/v1")
        assert "http://localhost:8000/v1" in out

    def test_comment_preserved(self) -> None:
        text = "# OPENAI_API_KEY=sk-1234567890abcdef"
        assert _mask_secrets(text) == text

    def test_non_secret_preserved(self) -> None:
        assert _mask_secrets("MODEL=gpt-5.4") == "MODEL=gpt-5.4"

    def test_redact_url_credentials_direct(self) -> None:
        out = _redact_url_credentials("postgresql://u:p@h/db")
        assert out == "postgresql://u:****@h/db"

    def test_hash_in_secret_value_not_leaked(self) -> None:
        # '#' must NOT be treated as a comment inside a value (bug-2).
        out = _mask_secrets('password = "p#ssw0rd1234"')
        assert "p#ssw0rd1234" not in out
        assert "ssw0rd" not in out

    def test_hash_in_db_url_password_not_leaked(self) -> None:
        out = _mask_secrets('url = "postgresql://turnstone:p#ss@h:5432/db"')
        assert "p#ss" not in out
        assert "turnstone:****@h" in out

    def test_yaml_style_secret_masked(self) -> None:
        out = _mask_secrets("POSTGRES_PASSWORD: hunter2s=long")
        assert "hunter2s=long" not in out

    def test_timestamp_line_untouched(self) -> None:
        # A log line with ':' but no config key must pass through verbatim.
        line = "2026-06-25 22:42:53 [warning] something happened"
        assert _mask_secrets(line) == line


class TestScrubToolOutput:
    def test_redacts_dsn_in_log(self) -> None:
        # A DSN in a connection-failure log line (no assignment key) is redacted.
        out = _scrub_tool_output("FATAL: connect to postgresql://u:hunter2pw@h/db failed")
        assert "hunter2pw" not in out
        assert "u:****@h" in out

    def test_drops_pem_block(self) -> None:
        text = "before\n-----BEGIN PRIVATE KEY-----\nSEKRIT\n-----END PRIVATE KEY-----\nafter"
        out = _scrub_tool_output(text)
        assert "SEKRIT" not in out
        assert "before" in out and "after" in out

    def test_masks_env_echo(self) -> None:
        # Env echo prints one VAR=value per line — the secret line is masked.
        out = _scrub_tool_output("startup env:\nTURNSTONE_JWT_SECRET=deadbeefdeadbeef\nready")
        assert "deadbeefdeadbeef" not in out


# ---------------------------------------------------------------------------
# read_file (scoped + masked)
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_existing_file(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        assert _tool_read_file(tmp_path, {"path": "a.txt"}) == "hello"

    def test_missing_file(self, tmp_path: Path) -> None:
        assert "file not found" in _tool_read_file(tmp_path, {"path": "nope.txt"})

    def test_traversal_blocked(self, tmp_path: Path) -> None:
        assert "escapes install directory" in _tool_read_file(
            tmp_path, {"path": "../../etc/passwd"}
        )

    def test_absolute_blocked(self, tmp_path: Path) -> None:
        assert "escapes install directory" in _tool_read_file(tmp_path, {"path": "/etc/passwd"})

    def test_secrets_masked_via_execute_tool(self, tmp_path: Path) -> None:
        # Masking is centralized at the execute_tool chokepoint.
        (tmp_path / ".env").write_text("TURNSTONE_JWT_SECRET=abcdef0123456789\n")
        out = execute_tool("read_file", {"path": ".env"}, tmp_path)
        assert "abcdef0123456789" not in out

    def test_refuses_key_file(self, tmp_path: Path) -> None:
        (tmp_path / "ca.key").write_text(
            "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n"
        )
        out = _tool_read_file(tmp_path, {"path": "ca.key"})
        assert "Refused" in out and "x" not in out

    def test_refuses_pem_content(self, tmp_path: Path) -> None:
        (tmp_path / "creds.txt").write_text(
            "-----BEGIN RSA PRIVATE KEY-----\nSEKRIT\n-----END RSA PRIVATE KEY-----\n"
        )
        out = _tool_read_file(tmp_path, {"path": "creds.txt"})
        assert "SEKRIT" not in out

    def test_caps_large_read(self, tmp_path: Path) -> None:
        (tmp_path / "big.log").write_text("A" * 200_000)
        out = _tool_read_file(tmp_path, {"path": "big.log"})
        assert "truncated" in out and len(out) < 100_000


# ---------------------------------------------------------------------------
# check_port / check_docker
# ---------------------------------------------------------------------------


class TestCheckPort:
    def test_in_use(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]
            assert "IN USE" in _tool_check_port({"port": port})

    def test_free(self) -> None:
        result = _tool_check_port({"port": 59321})
        assert "FREE" in result or "IN USE" in result

    def test_invalid(self) -> None:
        assert "Error" in _tool_check_port({"port": -1})


class TestCheckDocker:
    def test_installed(self) -> None:
        d = MagicMock(returncode=0, stdout="24.0.7")
        c = MagicMock(returncode=0, stdout="2.24.5")
        with patch("subprocess.run", side_effect=[d, c]):
            out = _tool_check_docker({})
        assert "Docker: installed" in out and "Docker Compose: installed" in out

    def test_not_installed(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert "NOT installed" in _tool_check_docker({})

    def test_daemon_down(self) -> None:
        d = MagicMock(returncode=1, stderr="Cannot connect to the Docker daemon")
        c = MagicMock(returncode=1)
        with patch("subprocess.run", side_effect=[d, c]):
            assert "NOT running" in _tool_check_docker({})


# ---------------------------------------------------------------------------
# Diagnostic tools build READ-ONLY command lines (no mutating verbs)
# ---------------------------------------------------------------------------


class TestDiagnosticToolsReadOnly:
    def _capture(self, fn, *call_args) -> list[str]:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, *a, **k):  # noqa: ANN001
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="ok", stderr="")

        with patch("turnstone.doctor.subprocess.run", side_effect=fake_run):
            fn(*call_args)
        return captured["cmd"]

    def test_compose_status_readonly(self, tmp_path: Path) -> None:
        cmd = self._capture(_tool_compose_status, tmp_path, {})
        assert cmd[:2] == ["docker", "compose"]
        assert "ps" in cmd
        assert not (set(cmd) & MUTATING_TOKENS)

    def test_compose_logs_readonly(self, tmp_path: Path) -> None:
        cmd = self._capture(_tool_compose_logs, tmp_path, {"service": "node-1", "tail": 50})
        assert "logs" in cmd and "node-1" in cmd
        assert not (set(cmd) & MUTATING_TOKENS)

    def test_systemd_status_readonly(self) -> None:
        cmd = self._capture(_tool_systemd_status, {"unit": "turnstone-server.service"})
        assert cmd[0] == "systemctl" and "status" in cmd
        assert not (set(cmd) & MUTATING_TOKENS)

    def test_journal_tail_readonly(self) -> None:
        cmd = self._capture(_tool_journal_tail, {"unit": "turnstone-server.service", "lines": 10})
        assert cmd[0] == "journalctl"
        assert not (set(cmd) & MUTATING_TOKENS)

    def test_compose_logs_clamps_tail(self, tmp_path: Path) -> None:
        cmd = self._capture(_tool_compose_logs, tmp_path, {"tail": 999999})
        # absurd tail falls back to the default of 100
        assert "100" in cmd

    def test_systemd_status_rejects_option_injection(self) -> None:
        # A model-supplied unit that looks like a systemctl global option is refused.
        out = _tool_systemd_status({"unit": "-Hattacker.example"})
        assert "invalid unit" in out.lower()

    def test_systemd_status_uses_option_terminator(self, tmp_path: Path) -> None:
        cmd = self._capture(_tool_systemd_status, {"unit": "turnstone-server.service"})
        # '--' must precede the unit so it can't be parsed as an option.
        assert "--" in cmd
        assert cmd.index("--") < cmd.index("turnstone-server.service")

    def test_compose_logs_rejects_dashed_service(self, tmp_path: Path) -> None:
        out = _tool_compose_logs(tmp_path, {"service": "--privileged"})
        assert "invalid service" in out.lower()


class TestHttpGetJsonSafety:
    def test_rejects_file_scheme(self) -> None:
        with pytest.raises(ValueError, match="non-http"):
            _http_get_json("file:///etc/passwd")

    def test_rejects_link_local_metadata(self) -> None:
        with pytest.raises(ValueError, match="link-local|metadata"):
            _http_get_json("http://169.254.169.254/latest/meta-data/")

    def test_allows_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # loopback must stay allowed (probing localhost /health is the job)
        import turnstone.doctor as doctor

        class _FakeResp:
            def __enter__(self) -> _FakeResp:
                return self

            def __exit__(self, *a: object) -> bool:
                return False

            def read(self) -> bytes:
                return b'{"ok": true}'

        monkeypatch.setattr(doctor.urllib.request, "urlopen", lambda *a, **k: _FakeResp())
        assert _http_get_json("http://localhost:8080/health") == {"ok": True}


class TestHttpHealthTool:
    def test_ok(self) -> None:
        from turnstone.doctor import _tool_http_health

        with patch(
            "turnstone.doctor._http_get_json", return_value={"status": "ok", "version": "1.7.0a2"}
        ):
            out = _tool_http_health({"url": "http://localhost:8080"})
        assert "ok" in out and "1.7.0a2" in out

    def test_unreachable(self) -> None:
        import urllib.error

        from turnstone.doctor import _tool_http_health

        with patch("turnstone.doctor._http_get_json", side_effect=urllib.error.URLError("refused")):
            out = _tool_http_health({"url": "http://localhost:8080"})
        assert "unreachable" in out


class TestCheckLlmBackendTool:
    def test_probe_summarized(self) -> None:
        from turnstone.doctor import _tool_check_llm_backend

        fake = {"reachable": True, "available_models": ["m"], "error": None}
        with patch("turnstone.core.model_registry.probe_model_endpoint", return_value=fake):
            out = _tool_check_llm_backend({"provider": "openai", "base_url": "http://x/v1"})
        assert "reachable" in out


class TestNodeHealth:
    def _capture_cmd(self, tmp_path: Path, kind: str, args: dict[str, object]) -> list[str]:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, *a, **k):  # noqa: ANN001
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout='{"status": "ok"}', stderr="")

        with patch("turnstone.doctor.subprocess.run", side_effect=fake_run):
            _tool_node_health(tmp_path, kind, args)
        return captured["cmd"]

    def test_compose_execs_fixed_readonly_snippet(self, tmp_path: Path) -> None:
        cmd = self._capture_cmd(tmp_path, "docker-compose", {"node": "node-1"})
        assert cmd[:2] == ["docker", "compose"]
        assert "exec" in cmd and "-T" in cmd and "node-1" in cmd
        # the executed command is the fixed read-only health fetch — not arbitrary
        assert cmd[-3] == "python" and cmd[-2] == "-c"
        snippet = cmd[-1]
        assert "urlopen" in snippet and "/health" in snippet
        assert "system" not in snippet and "Popen" not in snippet

    def test_install_type_override_uses_http(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}

        def fake_get(url: str, timeout: float = 5.0) -> dict[str, str]:
            captured["url"] = url
            return {"status": "ok", "version": "1.7.0a2"}

        monkeypatch.setattr("turnstone.doctor._http_get_json", fake_get)
        # primary_kind is compose, but the per-node override forces the http path
        out = _tool_node_health(
            tmp_path, "docker-compose", {"node": "10.0.0.5", "install_type": "systemd"}
        )
        assert "10.0.0.5:8080/health" in captured["url"]
        assert "1.7.0a2" in out

    def test_rejects_option_node(self, tmp_path: Path) -> None:
        assert "Error" in _tool_node_health(tmp_path, "docker-compose", {"node": "--rm"})

    def test_execute_tool_threads_primary_kind(self, tmp_path: Path) -> None:
        captured: dict[str, list[str]] = {}

        def fake_run(cmd, *a, **k):  # noqa: ANN001
            captured["cmd"] = cmd
            return MagicMock(returncode=0, stdout="{}", stderr="")

        with patch("turnstone.doctor.subprocess.run", side_effect=fake_run):
            execute_tool("node_health", {"node": "node-2"}, tmp_path, primary_kind="docker-compose")
        assert "exec" in captured["cmd"] and "node-2" in captured["cmd"]


class TestConsoleReframe:
    def test_healthy_nodes_not_called_down(self) -> None:
        vr = VersionReport(
            installed=__version__,
            image_tag="",
            node_versions={},
            cluster_versions=["1.7.0a2"],
            unreachable_nodes=["node-1", "node-2"],
            drift=False,
            upstream_stable="",
            upstream_experimental="",
            upstream_error="skipped",
            behind_stable=False,
            behind_experimental=False,
            console_reachable=True,
            console_nodes=10,
        )
        out = render_version_report(vr)
        assert "10 node(s) live" in out
        assert "node_health" in out
        assert "down" not in out.lower()  # never frame healthy-per-console nodes as down


# ---------------------------------------------------------------------------
# finish + execute_tool dispatch
# ---------------------------------------------------------------------------


class TestFinishAndDispatch:
    def test_finish_raises(self) -> None:
        with pytest.raises(_FinishError, match="All done"):
            _tool_finish({"summary": "All done"})

    def test_finish_default_summary(self) -> None:
        with pytest.raises(_FinishError) as exc:
            _tool_finish({})
        assert exc.value.summary == "Diagnosis complete."

    def test_unknown_tool(self, tmp_path: Path) -> None:
        assert "unknown tool" in execute_tool("nonexistent", {}, tmp_path)

    def test_dispatch_read_file(self, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("hi")
        assert execute_tool("read_file", {"path": "x.txt"}, tmp_path) == "hi"

    def test_finish_propagates_through_execute(self, tmp_path: Path) -> None:
        with pytest.raises(_FinishError):
            execute_tool("finish", {"summary": "x"}, tmp_path)


# ---------------------------------------------------------------------------
# family_of
# ---------------------------------------------------------------------------


class TestFamilyOf:
    @pytest.mark.parametrize(
        ("provider", "expected"),
        [
            ("openai", "openai"),
            ("openai-compatible", "openai"),
            ("xai", "openai"),
            ("anthropic", "anthropic"),
            ("anthropic-compatible", "anthropic"),
            ("Anthropic", "anthropic"),
            ("google", None),
            ("", None),
        ],
    )
    def test_mapping(self, provider: str, expected: str | None) -> None:
        assert family_of(provider) == expected


# ---------------------------------------------------------------------------
# Preflight helpers
# ---------------------------------------------------------------------------


class TestPreflightHelpers:
    def test_find_repo_root(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "turnstone"\n')
        sub = tmp_path / "turnstone" / "core"
        sub.mkdir(parents=True)
        assert _find_repo_root(sub) == tmp_path

    def test_find_repo_root_none(self, tmp_path: Path) -> None:
        assert _find_repo_root(tmp_path) is None

    def test_find_compose_files(self, tmp_path: Path) -> None:
        (tmp_path / "compose.yaml").write_text("services: {}\n")
        found = _find_compose_files(tmp_path, {"TURNSTONE_DIR": str(tmp_path / "none")}, None)
        assert (tmp_path / "compose.yaml").resolve() in found

    def test_parse_config_sections(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[auth]\njwt_secret = "x"\n'
            '[database]\nbackend = "postgresql"\n'
            'url = "postgresql://u:p@h/db"\n'
            '[api]\nbase_url = "http://localhost:8000/v1"\napi_key = "sk-test"\n'
        )
        sections, db, api = _parse_config_sections(cfg)
        assert "auth" in sections and "database" in sections and "api" in sections
        assert db["backend"] == "postgresql"
        assert db["url"] == "postgresql://u:p@h/db"
        assert api["base_url"] == "http://localhost:8000/v1"
        assert api["api_key"] == "sk-test"

    def test_discover_config_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "fakehome")
        cfg = tmp_path / "config.toml"
        cfg.write_text('[database]\nbackend = "sqlite"\n')
        found = _discover_config_files(tmp_path, {})
        assert any(cf.path == cfg.resolve() for cf in found)

    def test_resolve_db_config_config_wins_over_env(self) -> None:
        cf = ConfigFileInfo(
            path=Path("/x"), sections=["database"], db={"backend": "postgresql", "url": "u"}, api={}
        )
        out = _resolve_db_config([cf], {"TURNSTONE_DB_BACKEND": "sqlite"})
        assert out["backend"] == "postgresql"

    def test_resolve_db_config_env_fallback(self) -> None:
        out = _resolve_db_config(
            [], {"TURNSTONE_DB_BACKEND": "postgresql", "TURNSTONE_DB_URL": "u"}
        )
        assert out["backend"] == "postgresql" and out["url"] == "u"

    def test_parse_config_extracts_db_ssl(self, tmp_path: Path) -> None:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[database]\nbackend = "postgresql"\nsslmode = "verify-full"\n'
            'sslrootcert = "/c/ca.crt"\nsslcert = "/c/client.crt"\nsslkey = "/c/client.key"\n'
        )
        _sections, db, _api = _parse_config_sections(cfg)
        assert db["sslmode"] == "verify-full"
        assert db["sslrootcert"] == "/c/ca.crt"

    def test_resolve_db_config_carries_ssl(self) -> None:
        cf = ConfigFileInfo(
            path=Path("/x"),
            sections=["database"],
            db={"backend": "postgresql", "url": "u", "sslmode": "verify-full"},
            api={},
        )
        out = _resolve_db_config([cf], {"TURNSTONE_DB_SSLCERT": "/env/client.crt"})
        assert out["sslmode"] == "verify-full"  # config
        assert out["sslcert"] == "/env/client.crt"  # env fallback

    def test_relevant_env_redacts_secrets(self) -> None:
        out = _relevant_env(
            {"TURNSTONE_JWT_SECRET": "supersecret", "TURNSTONE_HOST_IP": "10.0.0.1"}
        )
        assert out["TURNSTONE_JWT_SECRET"] == "set (hidden)"
        assert out["TURNSTONE_HOST_IP"] == "10.0.0.1"

    def test_relevant_env_redacts_db_url_creds(self) -> None:
        out = _relevant_env({"TURNSTONE_DB_URL": "postgresql://u:pw@h/db"})
        # TURNSTONE_DB_URL is in the explicit scrub set → fully hidden
        assert out["TURNSTONE_DB_URL"] == "set (hidden)"

    def test_primary_kind_prefers_running_compose(self) -> None:
        kinds = ["git-source", "docker-compose"]
        assert _primary_kind(kinds, "node-1  running") == "docker-compose"

    def test_primary_kind_systemd_when_not_running_compose(self) -> None:
        kinds = ["docker-compose", "systemd"]
        assert _primary_kind(kinds, "") == "systemd"

    def test_derive_health_urls(self) -> None:
        urls = _derive_health_urls({})
        assert "http://localhost:8080/health" in urls
        assert "http://localhost:8090/health" in urls

    def test_read_api_creds_reads_parsed_api_field(self, tmp_path: Path) -> None:
        # perf-5: creds come from the already-parsed ConfigFileInfo.api (first
        # non-empty wins) — the file is never re-opened (it doesn't even exist here).
        cf = ConfigFileInfo(
            path=tmp_path / "config.toml",
            sections=["api"],
            db={},
            api={"base_url": "http://localhost:8000/v1", "api_key": "sk-cfg"},
        )
        profile = _make_profile(tmp_path, config_files=[cf])
        base_url, api_key = _read_api_creds(profile, {})
        assert base_url == "http://localhost:8000/v1"
        assert api_key == "sk-cfg"

    def test_read_api_creds_env_fallback(self, tmp_path: Path) -> None:
        profile = _make_profile(tmp_path, config_files=[])
        base_url, api_key = _read_api_creds(
            profile, {"LLM_BASE_URL": "http://env/v1", "OPENAI_API_KEY": "sk-env"}
        )
        assert base_url == "http://env/v1"
        assert api_key == "sk-env"

    def test_detect_install_profile_classifies_compose(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Direct classification: a compose file present, nothing else probing true.
        (tmp_path / "compose.yaml").write_text("services: {}\n")
        monkeypatch.setattr("turnstone.doctor._docker_available", lambda: False)
        monkeypatch.setattr("turnstone.doctor._systemd_units", lambda: [])
        monkeypatch.setattr("turnstone.doctor._find_repo_root", lambda p: None)
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "nohome")
        profile = detect_install_profile(tmp_path, {})
        assert "docker-compose" in profile.kinds
        assert isinstance(profile, InstallProfile)


# ---------------------------------------------------------------------------
# Reports never leak secrets
# ---------------------------------------------------------------------------


class TestReportNoSecretLeak:
    def test_profile_report_masks_db_password(self, tmp_path: Path) -> None:
        profile = _make_profile(
            tmp_path,
            db_config={"backend": "postgresql", "url": "postgresql://u:TOPSECRET@h/db", "path": ""},
            env_present={"TURNSTONE_JWT_SECRET": "set (hidden)"},
        )
        report = render_profile_report(profile)
        assert "TOPSECRET" not in report
        assert "u:****@h" in report

    def test_full_report_has_three_sections(self, tmp_path: Path) -> None:
        profile = _make_profile(tmp_path)
        vr = VersionReport(__version__, "", {}, [], [], False, "", "", "skipped", False, False)
        verdict = BackendVerdict(False, "no db")
        report = render_full_report(profile, vr, verdict)
        assert "## Install profile" in report
        assert "## Versions" in report
        assert "## LLM backend" in report


# ---------------------------------------------------------------------------
# open_storage (read-only; never creates a SQLite file)
# ---------------------------------------------------------------------------


class TestOpenStorage:
    def test_missing_sqlite_returns_error(self, tmp_path: Path) -> None:
        profile = _make_profile(
            tmp_path, db_config={"backend": "sqlite", "url": "", "path": str(tmp_path / "nope.db")}
        )
        storage, err = open_storage(profile)
        assert storage is None
        assert "no SQLite database file" in err
        # the message names the actual paths searched (not a hard-coded default)
        assert "nope.db" in err
        # crucially, it did not create the file
        assert not (tmp_path / "nope.db").exists()

    def test_read_only_no_migrations_no_create_tables(self, tmp_path: Path) -> None:
        # Diagnose-only: must neither migrate NOR create_all() against the live DB.
        profile = _make_profile(
            tmp_path,
            db_config={"backend": "postgresql", "url": "postgresql://u:p@h/db", "path": ""},
        )
        fake_init = MagicMock(return_value=MagicMock())
        with patch("turnstone.core.storage.init_storage", fake_init):
            open_storage(profile)
        assert fake_init.call_args.kwargs["run_migrations"] is False
        assert fake_init.call_args.kwargs["create_tables"] is False

    def test_forwards_db_ssl_params(self, tmp_path: Path) -> None:
        # An SSL/mTLS-required Postgres needs the [database] ssl* params forwarded.
        profile = _make_profile(
            tmp_path,
            db_config={
                "backend": "postgresql",
                "url": "postgresql://u:p@h/db",
                "path": "",
                "sslmode": "verify-full",
                "sslrootcert": "/c/ca.crt",
                "sslcert": "/c/client.crt",
                "sslkey": "/c/client.key",
            },
        )
        fake_init = MagicMock(return_value=MagicMock())
        with patch("turnstone.core.storage.init_storage", fake_init):
            open_storage(profile)
        kwargs = fake_init.call_args.kwargs
        assert kwargs["sslmode"] == "verify-full"
        assert kwargs["sslrootcert"] == "/c/ca.crt"
        assert kwargs["sslcert"] == "/c/client.crt"
        assert kwargs["sslkey"] == "/c/client.key"


# ---------------------------------------------------------------------------
# Self-configuring brain (§3)
# ---------------------------------------------------------------------------


class TestResolveDoctorBrain:
    def test_storage_none_falls_back(self, tmp_path: Path) -> None:
        brain, verdict = resolve_doctor_brain(_make_profile(tmp_path), None, "boom")
        assert brain is None
        assert verdict.ok is False
        assert "boom" in verdict.detail

    def _patch_resolution(self, monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
        from turnstone.core.model_registry import ModelConfig

        cfg = ModelConfig(
            alias="default",
            base_url="http://localhost:9/v1",
            api_key="dummy",
            model="test-model",
            context_window=8192,
            provider=provider,
        )
        registry = MagicMock()
        registry.resolve.return_value = (MagicMock(), "test-model", cfg)
        monkeypatch.setattr(
            "turnstone.core.config_store.ConfigStore",
            lambda **kw: MagicMock(get=lambda *a, **k: ""),
        )
        monkeypatch.setattr(
            "turnstone.core.model_registry.load_model_registry", lambda **kw: registry
        )

    def test_success(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_resolution(monkeypatch, "openai-compatible")
        monkeypatch.setattr("turnstone.doctor._validate_connection", lambda llm: (True, ""))
        brain, verdict = resolve_doctor_brain(_make_profile(tmp_path), MagicMock(), "")
        assert brain is not None
        assert brain.model == "test-model"
        assert verdict.ok is True

    def test_google_provider_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_resolution(monkeypatch, "google")
        brain, verdict = resolve_doctor_brain(_make_profile(tmp_path), MagicMock(), "")
        assert brain is None
        assert verdict.ok is False
        assert "google" in verdict.detail.lower()

    def test_connection_failure_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_resolution(monkeypatch, "openai")
        monkeypatch.setattr("turnstone.doctor._validate_connection", lambda llm: (False, "refused"))
        brain, verdict = resolve_doctor_brain(_make_profile(tmp_path), MagicMock(), "")
        assert brain is None
        assert "refused" in verdict.detail

    def test_no_model_configured_falls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "turnstone.core.config_store.ConfigStore",
            lambda **kw: MagicMock(get=lambda *a, **k: ""),
        )

        def boom(**kw: object) -> object:
            raise RuntimeError("no models")

        monkeypatch.setattr("turnstone.core.model_registry.load_model_registry", boom)
        brain, verdict = resolve_doctor_brain(_make_profile(tmp_path), MagicMock(), "")
        assert brain is None
        assert "no usable model" in verdict.detail


class TestResolveBrainIntegration:
    """End-to-end against an ephemeral SQLite DB seeded with one model."""

    def test_resolves_seeded_model(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from turnstone.core.storage import init_storage, reset_storage

        db_path = str(tmp_path / "doctor.db")
        reset_storage()
        st = init_storage("sqlite", path=db_path, run_migrations=True)
        try:
            st.create_model_definition(
                "def1",
                "default",
                "test-model",
                provider="openai-compatible",
                base_url="http://localhost:9/v1",
                api_key="dummy",
                context_window=8192,
                enabled=True,
            )
            monkeypatch.setattr("turnstone.doctor._validate_connection", lambda llm: (True, ""))
            profile = _make_profile(
                tmp_path, db_config={"backend": "sqlite", "url": "", "path": db_path}
            )
            brain, verdict = resolve_doctor_brain(profile, st, "")
            assert brain is not None
            assert brain.model == "test-model"
            assert verdict.ok is True
        finally:
            reset_storage()


# ---------------------------------------------------------------------------
# Version check (§4)
# ---------------------------------------------------------------------------


class TestCheckVersions:
    def test_offline_skips_upstream(self, tmp_path: Path) -> None:
        vr = check_versions(_make_profile(tmp_path), None, offline=True)
        assert vr.upstream_error == "skipped (--offline)"
        assert vr.installed == __version__

    def test_drift_detected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "n1", "url": "http://a"}, {"service_id": "n2", "url": "http://b"}]
            if t == "server"
            else []
        )
        versions = {"http://a": "1.6.9", "http://b": "1.7.0a2"}
        monkeypatch.setattr(
            "turnstone.doctor._fetch_health_version", lambda url: versions.get(url, "")
        )
        vr = check_versions(_make_profile(tmp_path), storage, offline=True)
        assert vr.drift is True
        assert set(vr.node_versions.values()) == {"1.6.9", "1.7.0a2"}

    def test_no_drift_when_uniform(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "n1", "url": "http://a"}] if t == "server" else []
        )
        monkeypatch.setattr("turnstone.doctor._fetch_health_version", lambda url: "1.7.0a2")
        vr = check_versions(_make_profile(tmp_path), storage, offline=True)
        assert vr.drift is False

    def test_unreachable_node_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "down", "url": "http://x"}] if t == "server" else []
        )
        monkeypatch.setattr("turnstone.doctor._fetch_health_version", lambda url: "")
        monkeypatch.setattr("turnstone.doctor._tls_cert_dir_present", lambda: False)
        vr = check_versions(_make_profile(tmp_path), storage, offline=True)
        assert "down" in vr.unreachable_nodes
        assert vr.mtls is False

    def test_mtls_detected_from_https_urls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # https advertise URLs ⇒ the node mesh runs mTLS; per-node probes can't auth.
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "node-1", "url": "https://node-1:8080"}] if t == "server" else []
        )
        monkeypatch.setattr("turnstone.doctor._fetch_health_version", lambda url: "")
        monkeypatch.setattr("turnstone.doctor._tls_cert_dir_present", lambda: False)
        vr = check_versions(_make_profile(tmp_path), storage, offline=True)
        assert vr.mtls is True
        assert "node-1" in vr.unreachable_nodes
        # the report reframes "unreachable" as an mTLS limitation, not "down"
        rendered = render_version_report(vr)
        assert "mTLS" in rendered

    def test_cert_dir_only_signals_mtls_when_storage_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A stray cert dir must NOT false-positive when storage is reachable + http.
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "n1", "url": "http://a"}] if t == "server" else []
        )
        monkeypatch.setattr("turnstone.doctor._fetch_health_version", lambda url: "1.7.0a2")
        monkeypatch.setattr("turnstone.doctor._tls_cert_dir_present", lambda: True)
        vr = check_versions(_make_profile(tmp_path), storage, offline=True)
        assert vr.mtls is False  # http URLs are authoritative; cert dir ignored
        # but with storage unreachable, the cert dir is the fallback signal
        vr2 = check_versions(_make_profile(tmp_path), None, offline=True)
        assert vr2.mtls is True

    def test_drift_via_console_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Nodes are registered but unreachable (container-internal URLs); the
        # console /health reports cluster-wide versions + drift.
        storage = MagicMock()
        storage.list_services.side_effect = lambda t, **k: (
            [{"service_id": "node-1", "url": "http://internal:8080"}] if t == "server" else []
        )
        monkeypatch.setattr("turnstone.doctor._fetch_health_version", lambda url: "")
        monkeypatch.setattr(
            "turnstone.doctor._probe_health",
            lambda url: {"versions": ["1.6.9", "1.7.0a2"], "version_drift": True},
        )
        profile = _make_profile(tmp_path, health_urls=["http://localhost:8090"])
        vr = check_versions(profile, storage, offline=True)
        assert vr.drift is True
        assert vr.cluster_versions == ["1.6.9", "1.7.0a2"]
        assert "node-1" in vr.unreachable_nodes

    def test_upstream_parse(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        tags = [{"name": "v1.6.8"}, {"name": "v1.7.0a2"}, {"name": "v1.6.9"}]
        monkeypatch.setattr("turnstone.doctor._http_get_json", lambda url, timeout=6.0: tags)
        vr = check_versions(_make_profile(tmp_path), None, offline=False)
        assert vr.upstream_stable == "1.6.9"
        assert vr.upstream_experimental == "1.7.0a2"

    def test_upstream_unreachable_degrades(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import urllib.error

        def boom(url: str, timeout: float = 6.0) -> object:
            raise urllib.error.URLError("no net")

        monkeypatch.setattr("turnstone.doctor._http_get_json", boom)
        vr = check_versions(_make_profile(tmp_path), None, offline=False)
        assert vr.upstream_error  # non-empty, but no exception raised


class TestComposeImageTag:
    def test_from_env_present(self, tmp_path: Path) -> None:
        profile = _make_profile(tmp_path, env_present={"TURNSTONE_IMAGE_TAG": "v1.7.0a2"})
        assert _compose_image_tag(profile) == "v1.7.0a2"

    def test_falls_back_to_dotenv_file(self, tmp_path: Path) -> None:
        # No env_present override → read TURNSTONE_IMAGE_TAG from the .env next to
        # the compose file.
        (tmp_path / "compose.yaml").write_text("services: {}\n")
        (tmp_path / ".env").write_text("TURNSTONE_IMAGE_TAG=v1.6.9\n")
        profile = _make_profile(tmp_path, compose_files=[tmp_path / "compose.yaml"])
        assert _compose_image_tag(profile) == "v1.6.9"


class TestRenderVersionReport:
    def _vr(self, **overrides: object) -> VersionReport:
        base: dict[str, object] = {
            "installed": __version__,
            "image_tag": "",
            "node_versions": {},
            "cluster_versions": [],
            "unreachable_nodes": [],
            "drift": False,
            "upstream_stable": "",
            "upstream_experimental": "",
            "upstream_error": "skipped",
            "behind_stable": False,
            "behind_experimental": False,
        }
        base.update(overrides)
        return VersionReport(**base)  # type: ignore[arg-type]

    def test_renders_per_node_versions(self) -> None:
        vr = self._vr(
            node_versions={"n1": "1.6.9", "n2": "1.7.0a2"},
            cluster_versions=["1.6.9", "1.7.0a2"],
            drift=True,
        )
        out = render_version_report(vr)
        assert "Per-node versions:" in out
        assert "n1=1.6.9" in out and "n2=1.7.0a2" in out

    def test_omits_per_node_line_when_empty(self) -> None:
        out = render_version_report(self._vr())
        assert "Per-node versions:" not in out


class TestVersionBehind:
    def test_behind(self) -> None:
        assert _version_behind("1.6.5", "1.6.9") is True

    def test_not_behind(self) -> None:
        assert _version_behind("1.7.0a2", "1.6.9") is False

    def test_missing_inputs(self) -> None:
        assert _version_behind("", "1.6.9") is False
        assert _version_behind("1.6.9", "") is False

    def test_unparseable(self) -> None:
        assert _version_behind("not-a-version", "1.6.9") is False


class TestUpstreamFetchParsing:
    def test_classifies_stable_vs_prerelease(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tags = [{"name": "v1.5.18"}, {"name": "v1.6.9"}, {"name": "v1.7.0a1"}, {"name": "v1.7.0a2"}]
        monkeypatch.setattr("turnstone.doctor._http_get_json", lambda url, timeout=6.0: tags)
        stable, experimental, err = _fetch_upstream_versions()
        assert stable == "1.6.9"
        assert experimental == "1.7.0a2"
        assert err == ""


# ---------------------------------------------------------------------------
# _DoctorLLM provider conversion
# ---------------------------------------------------------------------------


class TestDoctorLLMOpenAI:
    def test_text_response(self) -> None:
        llm = _DoctorLLM("openai", MagicMock(), "gpt-5.4")
        choice = MagicMock()
        choice.message.content = "Hello!"
        choice.message.tool_calls = None
        choice.finish_reason = "stop"
        llm.client.chat.completions.create.return_value = MagicMock(choices=[choice])
        content, tool_calls, reason = llm.complete([{"role": "user", "content": "hi"}], TOOLS)
        assert content == "Hello!" and tool_calls is None and reason == "stop"

    def test_tool_call_response(self) -> None:
        llm = _DoctorLLM("openai", MagicMock(), "gpt-5.4")
        tc = MagicMock()
        tc.id = "call_123"
        tc.function.name = "check_docker"
        tc.function.arguments = "{}"
        choice = MagicMock()
        choice.message.content = ""
        choice.message.tool_calls = [tc]
        choice.finish_reason = "tool_calls"
        llm.client.chat.completions.create.return_value = MagicMock(choices=[choice])
        _content, tool_calls, _reason = llm.complete([{"role": "user", "content": "x"}], TOOLS)
        assert tool_calls is not None and tool_calls[0]["function"]["name"] == "check_docker"

    def test_null_response_raises(self) -> None:
        llm = _DoctorLLM("openai", MagicMock(), "gpt-5.4")
        llm.client.chat.completions.create.return_value = None
        with pytest.raises(RuntimeError):
            llm.complete([{"role": "user", "content": "x"}], [])


class TestDoctorLLMAnthropic:
    def _make(self) -> _DoctorLLM:
        return _DoctorLLM("anthropic", MagicMock(), "claude-sonnet-4-6")

    def test_system_extracted(self) -> None:
        llm = self._make()
        resp = MagicMock()
        resp.content = [MagicMock(type="text", text="ok")]
        resp.stop_reason = "end_turn"
        llm.client.messages.create.return_value = resp
        llm.complete([{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], [])
        kwargs = llm.client.messages.create.call_args[1]
        assert kwargs["system"] == "sys"
        assert all(m["role"] != "system" for m in kwargs["messages"])

    def test_tool_result_converted(self) -> None:
        llm = self._make()
        resp = MagicMock()
        resp.content = [MagicMock(type="text", text="done")]
        resp.stop_reason = "end_turn"
        llm.client.messages.create.return_value = resp
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "check_docker", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "Docker: installed"},
        ]
        llm.complete(messages, TOOLS)
        api_messages = llm.client.messages.create.call_args[1]["messages"]
        found = any(
            isinstance(m.get("content"), list)
            and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in m["content"])
            for m in api_messages
        )
        assert found

    def test_tool_format_conversion(self) -> None:
        llm = self._make()
        resp = MagicMock()
        resp.content = [MagicMock(type="text", text="ok")]
        resp.stop_reason = "end_turn"
        llm.client.messages.create.return_value = resp
        llm.complete(
            [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}], TOOLS[:1]
        )
        api_tools = llm.client.messages.create.call_args[1]["tools"]
        assert api_tools[0]["name"] == "read_file" and "input_schema" in api_tools[0]


# ---------------------------------------------------------------------------
# Conversation loop
# ---------------------------------------------------------------------------


class TestConversationLoop:
    def test_quit_exits(self) -> None:
        llm = MagicMock(spec=_DoctorLLM)
        llm.complete.return_value = ("What's wrong?", None, "stop")
        with patch("builtins.input", return_value="quit"):
            _run_conversation(llm, Path("/tmp"), "ctx")

    def test_tool_calls_executed(self, tmp_path: Path) -> None:
        llm = MagicMock(spec=_DoctorLLM)
        llm.complete.side_effect = [
            (
                "",
                [
                    {
                        "id": "tc1",
                        "type": "function",
                        "function": {"name": "check_port", "arguments": '{"port": 8080}'},
                    }
                ],
                "tool_calls",
            ),
            ("Looks fine.", None, "stop"),
        ]
        with patch("builtins.input", return_value="quit"):
            _run_conversation(llm, tmp_path, "ctx")
        assert llm.complete.call_count == 2
        second = llm.complete.call_args_list[1][0][0]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "tc1"

    def test_finish_exits(self, tmp_path: Path) -> None:
        llm = MagicMock(spec=_DoctorLLM)
        llm.complete.return_value = (
            "",
            [
                {
                    "id": "f",
                    "type": "function",
                    "function": {"name": "finish", "arguments": '{"summary": "done"}'},
                }
            ],
            "tool_calls",
        )
        _run_conversation(llm, tmp_path, "ctx")
        assert llm.complete.call_count == 1

    def test_empty_input_skipped(self) -> None:
        llm = MagicMock(spec=_DoctorLLM)
        llm.complete.return_value = ("Ask me.", None, "stop")
        calls = {"n": 0}

        def fake_input(prompt: str = "") -> str:
            calls["n"] += 1
            return "" if calls["n"] <= 2 else "quit"

        with patch("builtins.input", side_effect=fake_input):
            _run_conversation(llm, Path("/tmp"), "ctx")


# ---------------------------------------------------------------------------
# Interactive provider selection (fallback)
# ---------------------------------------------------------------------------


class TestSelectProvider:
    def test_openai(self) -> None:
        with (
            patch("builtins.input", side_effect=["1", ""]),
            patch("getpass.getpass", return_value="sk-test"),
            patch("openai.OpenAI", return_value=MagicMock()),
        ):
            provider, _client, model = _select_provider()
        assert provider == "openai" and model == "gpt-5.4"

    def test_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with (
            patch("builtins.input", side_effect=["3", "http://localhost:8000/v1", "my-model"]),
            patch("getpass.getpass", return_value="none"),
            patch("openai.OpenAI", return_value=MagicMock()),
        ):
            provider, _client, model = _select_provider()
        assert provider == "openai" and model == "my-model"


# ---------------------------------------------------------------------------
# CLI report smoke
# ---------------------------------------------------------------------------


class TestReportCLI:
    def test_report_prints_sections(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from turnstone import doctor

        # Keep the smoke hermetic: no DB, no docker/systemd probing.
        monkeypatch.setattr(doctor, "open_storage", lambda profile: (None, "no db (test)"))
        monkeypatch.setattr(doctor, "_docker_available", lambda: False)
        monkeypatch.setattr(doctor, "_systemd_units", lambda: [])
        monkeypatch.setattr(
            sys, "argv", ["turnstone-doctor", "--report", "--offline", "--dir", str(tmp_path)]
        )
        doctor.main()
        out = capsys.readouterr().out
        assert "## Install profile" in out
        assert "## Versions" in out
        assert "## LLM backend" in out


# ---------------------------------------------------------------------------
# Constants / tool sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_system_prompt_is_diagnose_only(self) -> None:
        assert "DIAGNOSE-ONLY" in SYSTEM_PROMPT
        assert "Turnstone" in SYSTEM_PROMPT
        assert len(SYSTEM_PROMPT) > 500

    def test_all_tools_well_formed(self) -> None:
        for tool in TOOLS:
            assert tool["type"] == "function"
            fn = tool["function"]
            assert "name" in fn and "description" in fn
            assert fn["parameters"]["type"] == "object"

    def test_all_tools_have_implementations(self) -> None:
        from turnstone.doctor import TOOL_FUNCTIONS

        for tool in TOOLS:
            assert tool["function"]["name"] in TOOL_FUNCTIONS

    def test_expected_diagnostic_tools_present(self) -> None:
        names = {t["function"]["name"] for t in TOOLS}
        assert {
            "read_file",
            "compose_status",
            "compose_logs",
            "http_health",
            "check_llm_backend",
            "finish",
        } <= names
        # setup-only tools must be gone
        assert (
            "write_file" not in names
            and "write_compose" not in names
            and "generate_secret" not in names
        )
