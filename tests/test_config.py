"""Tests for turnstone.core.config — unified TOML config loading."""

import argparse

import turnstone.core.config as config_mod

apply_config = config_mod.apply_config
load_config = config_mod.load_config
set_config_path = config_mod.set_config_path


def _reset_cache():
    """Clear the module-level config cache between tests."""
    config_mod._cache = None
    config_mod._config_path = None


def test_load_config_missing_file(tmp_path):
    _reset_cache()
    set_config_path(str(tmp_path / "nope.toml"))
    assert load_config() == {}


def test_load_config_valid_toml(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\nhost = "10.0.0.1"\nport = 5432\nname = "turnstone"\n')
    set_config_path(str(cfg))
    result = load_config()
    assert result["database"]["host"] == "10.0.0.1"
    assert result["database"]["port"] == 5432
    assert result["database"]["name"] == "turnstone"


def test_load_config_section(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\nbase_url = "http://x:8000/v1"\n[database]\nhost = "y"\n')
    set_config_path(str(cfg))
    assert load_config("database") == {"host": "y"}
    assert load_config("api") == {"base_url": "http://x:8000/v1"}
    assert load_config("nonexistent") == {}


def test_load_config_invalid_toml(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not valid toml [[[")
    set_config_path(str(cfg))
    assert load_config() == {}


def test_load_config_warns_when_world_readable(tmp_path, caplog):
    """Secrets in config.toml — warn if anyone but the owner can read it."""
    import logging
    import os

    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\nurl = "postgresql+psycopg://u:secret@h/d"\n')
    os.chmod(cfg, 0o644)
    set_config_path(str(cfg))

    with caplog.at_level(logging.WARNING, logger="turnstone.core.config"):
        load_config()

    messages = [r.getMessage() for r in caplog.records]
    assert any("group/world-readable" in m for m in messages)


def test_load_config_quiet_when_mode_0600(tmp_path, caplog):
    import logging
    import os

    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[database]\nurl = "postgresql+psycopg://u:secret@h/d"\n')
    os.chmod(cfg, 0o600)
    set_config_path(str(cfg))

    with caplog.at_level(logging.WARNING, logger="turnstone.core.config"):
        load_config()

    messages = [r.getMessage() for r in caplog.records]
    assert not any("group/world-readable" in m for m in messages)


def test_load_config_caches(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\nbase_url = "http://first"\n')
    set_config_path(str(cfg))
    first = load_config()
    assert first["api"]["base_url"] == "http://first"

    # Change file — should NOT be re-read (cached)
    cfg.write_text('[api]\nbase_url = "http://second"\n')
    second = load_config()
    assert second["api"]["base_url"] == "http://first"


def test_apply_config_sets_defaults(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[server]\nhost = "0.0.0.0"\nport = 9090\n[api]\nbase_url = "http://custom/v1"\n'
    )
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--base-url", default="http://localhost:11434/v1")

    apply_config(parser, ["server", "api"])
    args = parser.parse_args([])

    assert args.host == "0.0.0.0"
    assert args.port == 9090
    assert args.base_url == "http://custom/v1"


def test_apply_config_cli_overrides(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[server]\nhost = "config-host"\nport = 7777\n')
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)

    apply_config(parser, ["server"])
    # CLI flag overrides config
    args = parser.parse_args(["--host", "cli-host"])

    assert args.host == "cli-host"  # CLI wins
    assert args.port == 7777  # config wins (no CLI override)


def test_apply_config_missing_keys_keep_defaults(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[server]\nhost = "only-host"\n')  # no port
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)

    apply_config(parser, ["server"])
    args = parser.parse_args([])

    assert args.host == "only-host"
    assert args.port == 8080  # original default kept


def test_apply_config_no_file(tmp_path):
    _reset_cache()
    set_config_path(str(tmp_path / "nope.toml"))

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")

    apply_config(parser, ["server"])
    args = parser.parse_args([])
    assert args.host == "localhost"


def test_apply_config_model_section(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model]\nname = "qwen-72b"\ntemperature = 0.3\n')
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None)
    parser.add_argument("--temperature", type=float, default=0.5)

    apply_config(parser, ["model"])
    args = parser.parse_args([])

    assert args.model == "qwen-72b"
    assert args.temperature == 0.3


def _reset_searxng_cache():
    config_mod._searxng_url = None
    config_mod._searxng_url_loaded = False
    config_mod._searxng_engines = None
    config_mod._searxng_engines_loaded = False


def test_searxng_url_from_config(tmp_path, monkeypatch):
    """get_searxng_url() reads from config.toml [tools] searxng_url."""
    _reset_cache()
    _reset_searxng_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text('[tools]\nsearxng_url = "http://searx.local:8080"\n')
    set_config_path(str(cfg))
    monkeypatch.delenv("TURNSTONE_SEARXNG_URL", raising=False)

    assert config_mod.get_searxng_url() == "http://searx.local:8080"


def test_searxng_url_fallback_to_env(tmp_path, monkeypatch):
    """get_searxng_url() falls back to $TURNSTONE_SEARXNG_URL."""
    _reset_cache()
    _reset_searxng_cache()

    # Config exists but no searxng_url in it
    cfg = tmp_path / "config.toml"
    cfg.write_text("[tools]\n")
    set_config_path(str(cfg))
    monkeypatch.setenv("TURNSTONE_SEARXNG_URL", "http://env-searx:8080")

    assert config_mod.get_searxng_url() == "http://env-searx:8080"


def test_searxng_url_none_when_unset(tmp_path, monkeypatch):
    """get_searxng_url() returns None when neither config nor env is set."""
    _reset_cache()
    _reset_searxng_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text("[tools]\n")
    set_config_path(str(cfg))
    monkeypatch.delenv("TURNSTONE_SEARXNG_URL", raising=False)

    assert config_mod.get_searxng_url() is None


def test_searxng_engines_config_wins_over_env(tmp_path, monkeypatch):
    """get_searxng_engines(): config.toml [tools] searxng_engines wins over env."""
    _reset_cache()
    _reset_searxng_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text('[tools]\nsearxng_engines = "duckduckgo,wikipedia"\n')
    set_config_path(str(cfg))
    monkeypatch.setenv("TURNSTONE_SEARXNG_ENGINES", "ignored")

    assert config_mod.get_searxng_engines() == "duckduckgo,wikipedia"


def test_searxng_engines_default_empty(tmp_path, monkeypatch):
    """get_searxng_engines() defaults to '' when nothing is configured."""
    _reset_cache()
    _reset_searxng_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text("[tools]\n")
    set_config_path(str(cfg))
    monkeypatch.delenv("TURNSTONE_SEARXNG_ENGINES", raising=False)

    assert config_mod.get_searxng_engines() == ""


def _reset_workspace_cache():
    config_mod._workspace_dir = None
    config_mod._workspace_dir_loaded = False


def test_workspace_dir_from_config(tmp_path, monkeypatch):
    """get_workspace_dir() reads from config.toml [tools] workspace_dir."""
    _reset_cache()
    _reset_workspace_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text('[tools]\nworkspace_dir = "/srv/projects"\n')
    set_config_path(str(cfg))
    monkeypatch.delenv("TURNSTONE_WORKSPACE", raising=False)

    assert config_mod.get_workspace_dir() == "/srv/projects"


def test_workspace_dir_config_wins_over_env(tmp_path, monkeypatch):
    """get_workspace_dir(): config.toml [tools] workspace_dir wins over env."""
    _reset_cache()
    _reset_workspace_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text('[tools]\nworkspace_dir = "/srv/projects"\n')
    set_config_path(str(cfg))
    monkeypatch.setenv("TURNSTONE_WORKSPACE", "/ignored")

    assert config_mod.get_workspace_dir() == "/srv/projects"


def test_workspace_dir_fallback_to_env(tmp_path, monkeypatch):
    """get_workspace_dir() falls back to $TURNSTONE_WORKSPACE."""
    _reset_cache()
    _reset_workspace_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text("[tools]\n")
    set_config_path(str(cfg))
    monkeypatch.setenv("TURNSTONE_WORKSPACE", "/workspace")

    assert config_mod.get_workspace_dir() == "/workspace"


def test_workspace_dir_none_when_unset(tmp_path, monkeypatch):
    """get_workspace_dir() returns None when neither config nor env is set."""
    _reset_cache()
    _reset_workspace_cache()

    cfg = tmp_path / "config.toml"
    cfg.write_text("[tools]\n")
    set_config_path(str(cfg))
    monkeypatch.delenv("TURNSTONE_WORKSPACE", raising=False)

    assert config_mod.get_workspace_dir() is None


def test_apply_config_judge_section(tmp_path):
    """apply_config() loads [judge] section and maps to argparse dests."""
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[judge]\n"
        "enabled = true\n"
        'model = "gpt-5"\n'
        "confidence_threshold = 0.85\n"
        "timeout = 30.0\n"
        "read_only_tools = false\n"
    )
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", dest="judge_enabled", action="store_true", default=False)
    parser.add_argument("--judge-model", dest="judge_model", default="")
    parser.add_argument("--judge-confidence", dest="judge_confidence", type=float, default=0.7)
    parser.add_argument("--judge-timeout", dest="judge_timeout", type=float, default=60.0)
    parser.add_argument("--judge-read-only-tools", dest="judge_read_only_tools", default=True)

    apply_config(parser, ["judge"])
    args = parser.parse_args([])

    assert args.judge_enabled is True
    assert args.judge_model == "gpt-5"
    assert args.judge_confidence == 0.85
    assert args.judge_timeout == 30.0
    assert args.judge_read_only_tools is False


def test_apply_config_judge_cli_overrides(tmp_path):
    """CLI flags override config.toml [judge] values."""
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text("[judge]\nenabled = true\nconfidence_threshold = 0.85\n")
    set_config_path(str(cfg))

    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", dest="judge_enabled", action="store_true", default=False)
    parser.add_argument("--no-judge", dest="judge_enabled", action="store_false")
    parser.add_argument("--judge-confidence", dest="judge_confidence", type=float, default=0.7)

    apply_config(parser, ["judge"])
    args = parser.parse_args(["--no-judge"])

    assert args.judge_enabled is False  # CLI wins
    assert args.judge_confidence == 0.85  # config wins (no CLI override)


def test_set_config_path_overrides_default(tmp_path):
    """set_config_path() overrides the default config location."""
    _reset_cache()
    cfg = tmp_path / "custom.toml"
    cfg.write_text('[api]\nbase_url = "http://custom:9999"\n')
    set_config_path(str(cfg))
    assert load_config("api") == {"base_url": "http://custom:9999"}


def test_env_var_overrides_default(tmp_path, monkeypatch):
    """$TURNSTONE_CONFIG env var overrides the default config location."""
    _reset_cache()
    cfg = tmp_path / "env.toml"
    cfg.write_text('[api]\nbase_url = "http://env:7777"\n')
    monkeypatch.setenv("TURNSTONE_CONFIG", str(cfg))
    assert load_config("api") == {"base_url": "http://env:7777"}


def test_set_config_path_overrides_env_var(tmp_path, monkeypatch):
    """set_config_path() takes precedence over $TURNSTONE_CONFIG."""
    _reset_cache()
    env_cfg = tmp_path / "env.toml"
    env_cfg.write_text('[api]\nbase_url = "http://env"\n')
    monkeypatch.setenv("TURNSTONE_CONFIG", str(env_cfg))

    explicit_cfg = tmp_path / "explicit.toml"
    explicit_cfg.write_text('[api]\nbase_url = "http://explicit"\n')
    set_config_path(str(explicit_cfg))

    assert load_config("api") == {"base_url": "http://explicit"}
