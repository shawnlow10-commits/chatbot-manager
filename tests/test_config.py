"""Unit tests for the configuration loader."""

import os
import tempfile

import pytest
import yaml

from chatbot_monitor.config import AppConfig, ConfigError, load_config


def _make_valid_config() -> dict:
    """Return a minimal valid configuration dictionary."""
    return {
        "webhook_secret": "test-secret-123",
        "nim": {
            "api_key": "nim-key-456",
            "base_url": "https://integrate.api.nvidia.com/v1",
            "model": "meta/llama-3.1-70b-instruct",
            "timeout_seconds": 30,
        },
        "telegram": {
            "bot_token": "bot123:ABC-DEF",
            "chat_id": "-1001234567890",
        },
        "digest": {
            "schedule": "0 8 * * *",
        },
        "alert_defaults": {
            "dropoff_rate_pct": 50,
            "low_volume_pct": 50,
            "consecutive_errors": 3,
            "consecutive_neg_sentiment": 3,
            "persistence_count": 3,
            "cooldown_minutes": 60,
        },
        "db_path": "data/monitor.db",
        "clients": [
            {
                "client_id": "bot_realestate",
                "display_name": "Real Estate Bot",
                "thresholds": {"dropoff_rate_pct": 40},
                "active_hours": {
                    "start_time": "08:00",
                    "end_time": "22:00",
                    "timezone": "America/Sao_Paulo",
                    "days": [0, 1, 2, 3, 4],
                },
            }
        ],
    }


def _write_config(config_data: dict, path: str) -> str:
    """Write config data to a YAML file and return the path."""
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config_data, f)
    return path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Remove secret env vars so they don't interfere with test assertions."""
    for var in ("WEBHOOK_SECRET", "NIM_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)


class TestLoadConfigSuccess:
    """Tests for successful configuration loading."""

    def test_loads_valid_config(self, tmp_path):
        config_data = _make_valid_config()
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        config = load_config(config_file)

        assert isinstance(config, AppConfig)
        assert config.webhook_secret == "test-secret-123"
        assert config.nim_api_key == "nim-key-456"
        assert config.nim_base_url == "https://integrate.api.nvidia.com/v1"
        assert config.nim_model == "meta/llama-3.1-70b-instruct"
        assert config.telegram_bot_token == "bot123:ABC-DEF"
        assert config.telegram_chat_id == "-1001234567890"
        assert config.digest_schedule == "0 8 * * *"
        assert config.db_path == "data/monitor.db"

    def test_parses_clients(self, tmp_path):
        config_data = _make_valid_config()
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        config = load_config(config_file)

        assert "bot_realestate" in config.clients
        client = config.clients["bot_realestate"]
        assert client.client_id == "bot_realestate"
        assert client.display_name == "Real Estate Bot"
        assert client.thresholds.dropoff_rate_pct == 40
        # Inherited from defaults
        assert client.thresholds.cooldown_minutes == 60
        assert client.active_hours is not None
        assert client.active_hours.start_time == "08:00"
        assert client.active_hours.timezone == "America/Sao_Paulo"

    def test_client_without_active_hours(self, tmp_path):
        config_data = _make_valid_config()
        config_data["clients"] = [
            {
                "client_id": "bot_simple",
                "display_name": "Simple Bot",
                "thresholds": {},
            }
        ]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        config = load_config(config_file)

        client = config.clients["bot_simple"]
        assert client.active_hours is None
        # Uses defaults when thresholds is empty
        assert client.thresholds.dropoff_rate_pct == 50

    def test_empty_clients_list(self, tmp_path):
        config_data = _make_valid_config()
        config_data["clients"] = []
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        config = load_config(config_file)

        assert config.clients == {}

    def test_no_clients_key(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["clients"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        config = load_config(config_file)

        assert config.clients == {}


class TestEnvVarOverrides:
    """Tests for environment variable precedence over YAML values."""

    def test_env_var_overrides_yaml_secret(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_data["webhook_secret"] = "yaml-secret"
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        monkeypatch.setenv("WEBHOOK_SECRET", "env-secret-override")

        config = load_config(config_file)

        assert config.webhook_secret == "env-secret-override"

    def test_env_var_overrides_nim_api_key(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        monkeypatch.setenv("NIM_API_KEY", "env-nim-key")

        config = load_config(config_file)

        assert config.nim_api_key == "env-nim-key"

    def test_env_var_overrides_telegram_token(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-bot-token")

        config = load_config(config_file)

        assert config.telegram_bot_token == "env-bot-token"

    def test_env_var_overrides_telegram_chat_id(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        monkeypatch.setenv("TELEGRAM_CHAT_ID", "-99999")

        config = load_config(config_file)

        assert config.telegram_chat_id == "-99999"

    def test_placeholder_resolved_from_env(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_data["webhook_secret"] = "${WEBHOOK_SECRET}"
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        monkeypatch.setenv("WEBHOOK_SECRET", "resolved-from-env")

        config = load_config(config_file)

        assert config.webhook_secret == "resolved-from-env"


class TestValidationErrors:
    """Tests for configuration validation failures."""

    def test_missing_file_raises_config_error(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/path/config.yaml")

    def test_malformed_yaml_raises_config_error(self, tmp_path):
        config_file = str(tmp_path / "config.yaml")
        with open(config_file, "w") as f:
            f.write("{ invalid yaml: [unclosed")

        with pytest.raises(ConfigError, match="Malformed YAML"):
            load_config(config_file)

    def test_non_mapping_yaml_raises_config_error(self, tmp_path):
        config_file = str(tmp_path / "config.yaml")
        with open(config_file, "w") as f:
            f.write("- just\n- a\n- list\n")

        with pytest.raises(ConfigError, match="must contain a YAML mapping"):
            load_config(config_file)

    def test_missing_webhook_secret(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["webhook_secret"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="webhook_secret"):
            load_config(config_file)

    def test_missing_nim_api_key(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["nim"]["api_key"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="nim.api_key"):
            load_config(config_file)

    def test_missing_telegram_bot_token(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["telegram"]["bot_token"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="telegram.bot_token"):
            load_config(config_file)

    def test_missing_digest_schedule(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["digest"]["schedule"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="digest.schedule"):
            load_config(config_file)

    def test_unresolved_placeholder_raises_error(self, tmp_path, monkeypatch):
        config_data = _make_valid_config()
        config_data["webhook_secret"] = "${UNSET_VAR}"
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        # Make sure env var is not set
        monkeypatch.delenv("UNSET_VAR", raising=False)
        monkeypatch.delenv("WEBHOOK_SECRET", raising=False)

        with pytest.raises(ConfigError, match="UNSET_VAR is not set"):
            load_config(config_file)

    def test_client_missing_client_id(self, tmp_path):
        config_data = _make_valid_config()
        config_data["clients"] = [
            {"display_name": "No ID Bot", "thresholds": {}}
        ]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="client_id"):
            load_config(config_file)

    def test_client_missing_display_name(self, tmp_path):
        config_data = _make_valid_config()
        config_data["clients"] = [
            {"client_id": "bot_no_name", "thresholds": {}}
        ]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError, match="display_name"):
            load_config(config_file)

    def test_multiple_errors_reported(self, tmp_path):
        config_data = _make_valid_config()
        del config_data["webhook_secret"]
        del config_data["nim"]["api_key"]
        config_file = str(tmp_path / "config.yaml")
        _write_config(config_data, config_file)

        with pytest.raises(ConfigError) as exc_info:
            load_config(config_file)

        error_msg = str(exc_info.value)
        assert "webhook_secret" in error_msg
        assert "nim.api_key" in error_msg
