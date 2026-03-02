"""Tests for turnstone.core.config — unified TOML config loading."""

import argparse

import turnstone.core.config as config_mod
from turnstone.core.config import apply_config, load_config


def _reset_cache():
    """Clear the module-level config cache between tests."""
    config_mod._cache = None


def test_load_config_missing_file(tmp_path, monkeypatch):
    _reset_cache()
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "nope.toml")
    assert load_config() == {}


def test_load_config_valid_toml(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[redis]\nhost = "10.0.0.1"\nport = 6380\npassword = "secret"\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    result = load_config()
    assert result["redis"]["host"] == "10.0.0.1"
    assert result["redis"]["port"] == 6380
    assert result["redis"]["password"] == "secret"


def test_load_config_section(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\nbase_url = "http://x:8000/v1"\n[redis]\nhost = "y"\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    assert load_config("redis") == {"host": "y"}
    assert load_config("api") == {"base_url": "http://x:8000/v1"}
    assert load_config("nonexistent") == {}


def test_load_config_invalid_toml(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not valid toml [[[")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    assert load_config() == {}


def test_load_config_caches(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\nbase_url = "http://first"\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    first = load_config()
    assert first["api"]["base_url"] == "http://first"

    # Change file — should NOT be re-read (cached)
    cfg.write_text('[api]\nbase_url = "http://second"\n')
    second = load_config()
    assert second["api"]["base_url"] == "http://first"


def test_apply_config_sets_defaults(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[redis]\nhost = "redis.local"\nport = 7777\npassword = "pw"\n'
        '[bridge]\nserver_url = "http://bridge:9090"\n'
    )
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)

    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-password", default=None)
    parser.add_argument("--server-url", default="http://localhost:8080")

    apply_config(parser, ["redis", "bridge"])
    args = parser.parse_args([])

    assert args.redis_host == "redis.local"
    assert args.redis_port == 7777
    assert args.redis_password == "pw"
    assert args.server_url == "http://bridge:9090"


def test_apply_config_cli_overrides(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[redis]\nhost = "config-host"\nport = 7777\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)

    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)

    apply_config(parser, ["redis"])
    # CLI flag overrides config
    args = parser.parse_args(["--redis-host", "cli-host"])

    assert args.redis_host == "cli-host"  # CLI wins
    assert args.redis_port == 7777  # config wins (no CLI override)


def test_apply_config_missing_keys_keep_defaults(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[redis]\nhost = "only-host"\n')  # no port, no password
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)

    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-host", default="localhost")
    parser.add_argument("--redis-port", type=int, default=6379)
    parser.add_argument("--redis-password", default=None)

    apply_config(parser, ["redis"])
    args = parser.parse_args([])

    assert args.redis_host == "only-host"
    assert args.redis_port == 6379  # original default kept
    assert args.redis_password is None  # original default kept


def test_apply_config_no_file(tmp_path, monkeypatch):
    _reset_cache()
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "nope.toml")

    parser = argparse.ArgumentParser()
    parser.add_argument("--redis-host", default="localhost")

    apply_config(parser, ["redis"])
    args = parser.parse_args([])
    assert args.redis_host == "localhost"


def test_apply_config_model_section(tmp_path, monkeypatch):
    _reset_cache()
    cfg = tmp_path / "config.toml"
    cfg.write_text('[model]\nname = "qwen-72b"\ntemperature = 0.3\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)

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
    import turnstone.core.memory as mem

    mem._tavily_key = None
    mem._tavily_key_loaded = False

    cfg = tmp_path / "config.toml"
    cfg.write_text('[api]\ntavily_key = "tvly-from-config"\n')
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    key = mem.get_tavily_key()
    assert key == "tvly-from-config"


def test_tavily_key_fallback_to_env(tmp_path, monkeypatch):
    """get_tavily_key() falls back to $TAVILY_API_KEY env var."""
    _reset_cache()
    import turnstone.core.memory as mem

    mem._tavily_key = None
    mem._tavily_key_loaded = False

    # Config exists but no tavily_key in it
    cfg = tmp_path / "config.toml"
    cfg.write_text("[api]\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-from-env")

    key = mem.get_tavily_key()
    assert key == "tvly-from-env"
