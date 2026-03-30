"""Tests for turnstone.core.config — unified TOML config loading."""

import argparse

import turnstone.core.config as config_mod
from turnstone.core.config import apply_config, load_config, set_config_path


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
    cfg.write_text('[redis]\nhost = "10.0.0.1"\nport = 6380\npassword = "secret"\n')
    set_config_path(str(cfg))
    result = load_config()
    assert result["redis"]["host"] == "10.0.0.1"
    assert result["redis"]["port"] == 6380
    assert result["redis"]["password"] == "secret"


def test_load_config_section(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\nbase_url = "http://x:8000/v1"\n[redis]\nhost = "y"\n')
    set_config_path(str(cfg))
    assert load_config("redis") == {"host": "y"}
    assert load_config("api") == {"base_url": "http://x:8000/v1"}
    assert load_config("nonexistent") == {}


def test_load_config_invalid_toml(tmp_path):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not valid toml [[[")
    set_config_path(str(cfg))
    assert load_config() == {}


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


def test_tavily_key_from_config(tmp_path, monkeypatch):
    """get_tavily_key() reads from config.toml [api] tavily_key."""
    _reset_cache()
    config_mod._tavily_key = None
    config_mod._tavily_key_loaded = False

    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\ntavily_key = "tvly-from-config"\n')
    set_config_path(str(cfg))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    key = config_mod.get_tavily_key()
    assert key == "tvly-from-config"


def test_tavily_key_fallback_to_env(tmp_path, monkeypatch):
    """get_tavily_key() falls back to $TAVILY_API_KEY env var."""
    _reset_cache()
    config_mod._tavily_key = None
    config_mod._tavily_key_loaded = False

    # Config exists but no tavily_key in it
    cfg = tmp_path / "config.toml"
    cfg.write_text("[api]\n")
    set_config_path(str(cfg))
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")

    key = config_mod.get_tavily_key()
    assert key == "tvly-from-env"


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
