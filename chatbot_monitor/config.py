"""Configuration loading from YAML and environment variables."""

import logging
import os
import re
import sys
from dataclasses import dataclass, field

import yaml

from chatbot_monitor.models import ActiveHours, AlertThresholds

logger = logging.getLogger(__name__)

# Pattern to match ${ENV_VAR_NAME} placeholders in YAML values
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# Environment variable names that override YAML values for secrets
_SECRET_ENV_OVERRIDES = {
    "WEBHOOK_SECRET": "webhook_secret",
    "NIM_API_KEY": "nim.api_key",
    "TELEGRAM_BOT_TOKEN": "telegram.bot_token",
    "TELEGRAM_CHAT_ID": "telegram.chat_id",
}


@dataclass
class ClientConfig:
    """Configuration for a single client/bot."""

    client_id: str
    display_name: str
    thresholds: AlertThresholds
    active_hours: ActiveHours | None


@dataclass
class AppConfig:
    """Application-wide configuration."""

    webhook_secret: str
    nim_api_key: str
    nim_base_url: str
    nim_model: str
    telegram_bot_token: str
    telegram_chat_id: str
    digest_schedule: str  # cron expression
    alert_defaults: AlertThresholds
    clients: dict[str, ClientConfig] = field(default_factory=dict)
    db_path: str = "data/monitor.db"


class ConfigError(Exception):
    """Raised when configuration is invalid or incomplete."""

    pass


def _resolve_env_placeholder(value: str) -> str:
    """Resolve ${ENV_VAR_NAME} placeholders in a string value.

    If the entire value is a placeholder (e.g. '${WEBHOOK_SECRET}'),
    return the env var value or the raw placeholder if not set.
    If the value contains embedded placeholders, substitute each one.
    """
    if not isinstance(value, str):
        return value

    def replacer(match: re.Match) -> str:
        env_name = match.group(1)
        return os.environ.get(env_name, match.group(0))

    return _ENV_VAR_PATTERN.sub(replacer, value)


def _get_nested(data: dict, dotted_path: str):
    """Get a value from a nested dict using dot notation (e.g. 'nim.api_key')."""
    keys = dotted_path.split(".")
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _apply_env_overrides(data: dict) -> dict:
    """Apply environment variable overrides for secret values.

    Environment variables take precedence over YAML values.
    """
    for env_name, yaml_path in _SECRET_ENV_OVERRIDES.items():
        env_value = os.environ.get(env_name)
        if env_value is not None:
            keys = yaml_path.split(".")
            current = data
            for key in keys[:-1]:
                if key not in current or not isinstance(current[key], dict):
                    current[key] = {}
                current = current[key]
            current[keys[-1]] = env_value

    return data


def _resolve_all_placeholders(data):
    """Recursively resolve all ${ENV_VAR} placeholders in the config data."""
    if isinstance(data, str):
        return _resolve_env_placeholder(data)
    elif isinstance(data, dict):
        return {k: _resolve_all_placeholders(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_all_placeholders(item) for item in data]
    return data


def _validate_required_global_keys(data: dict) -> list[str]:
    """Validate that all required global configuration keys are present and non-empty.

    Returns a list of error messages for missing/invalid keys.
    """
    errors = []

    required_keys = [
        ("webhook_secret", "webhook_secret"),
        ("nim.api_key", "nim.api_key"),
        ("nim.base_url", "nim.base_url"),
        ("nim.model", "nim.model"),
        ("telegram.bot_token", "telegram.bot_token"),
        ("telegram.chat_id", "telegram.chat_id"),
        ("digest.schedule", "digest.schedule"),
    ]

    for dotted_path, display_name in required_keys:
        value = _get_nested(data, dotted_path)
        if value is None or value == "":
            errors.append(f"Missing required configuration: {display_name}")
        elif isinstance(value, str) and _ENV_VAR_PATTERN.fullmatch(value):
            # Value is still an unresolved placeholder like ${SOME_VAR}
            env_name = _ENV_VAR_PATTERN.fullmatch(value).group(1)
            errors.append(
                f"Missing required configuration: {display_name} "
                f"(environment variable {env_name} is not set)"
            )

    return errors


def _validate_clients(data: dict) -> list[str]:
    """Validate per-client configuration entries.

    Returns a list of error messages for invalid client entries.
    """
    errors = []
    clients_data = data.get("clients")

    if clients_data is None:
        # clients section is optional — empty is valid
        return errors

    if not isinstance(clients_data, list):
        errors.append("'clients' must be a list of client configurations")
        return errors

    for i, client in enumerate(clients_data):
        if not isinstance(client, dict):
            errors.append(f"Client entry {i} is not a valid mapping")
            continue

        if not client.get("client_id"):
            errors.append(
                f"Client entry {i} is missing required field: client_id"
            )
        if not client.get("display_name"):
            errors.append(
                f"Client entry {i} is missing required field: display_name"
            )

    return errors


def _parse_client_config(
    client_data: dict, alert_defaults: AlertThresholds
) -> ClientConfig:
    """Parse a single client configuration entry."""
    # Parse thresholds — merge with defaults
    thresholds_data = client_data.get("thresholds", {})
    if thresholds_data:
        # Start from defaults and override with client-specific values
        defaults_dict = alert_defaults.model_dump()
        defaults_dict.update(thresholds_data)
        thresholds = AlertThresholds(**defaults_dict)
    else:
        thresholds = alert_defaults.model_copy()

    # Parse active hours
    active_hours_data = client_data.get("active_hours")
    active_hours = None
    if active_hours_data:
        active_hours = ActiveHours(**active_hours_data)

    return ClientConfig(
        client_id=client_data["client_id"],
        display_name=client_data["display_name"],
        thresholds=thresholds,
        active_hours=active_hours,
    )


def load_config(yaml_path: str = "config.yaml") -> AppConfig:
    """Load configuration from YAML file with environment variable overrides.

    The loading process:
    1. Read and parse the YAML file
    2. Apply environment variable overrides for secrets
    3. Resolve any remaining ${ENV_VAR} placeholders in values
    4. Validate all required keys are present
    5. Validate per-client entries
    6. Construct and return AppConfig

    Raises:
        ConfigError: If the config file is unreadable, malformed, or missing
                     required values.
        SystemExit: Logs error and exits if configuration is invalid.
    """
    # Step 1: Read and parse YAML
    try:
        with open(yaml_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        msg = f"Configuration file not found: {yaml_path}"
        logger.error(msg)
        raise ConfigError(msg)
    except yaml.YAMLError as e:
        msg = f"Malformed YAML in configuration file {yaml_path}: {e}"
        logger.error(msg)
        raise ConfigError(msg)
    except OSError as e:
        msg = f"Unable to read configuration file {yaml_path}: {e}"
        logger.error(msg)
        raise ConfigError(msg)

    if not isinstance(data, dict):
        msg = f"Configuration file {yaml_path} must contain a YAML mapping at the top level"
        logger.error(msg)
        raise ConfigError(msg)

    # Step 2: Apply environment variable overrides for secrets
    data = _apply_env_overrides(data)

    # Step 3: Resolve remaining ${ENV_VAR} placeholders
    data = _resolve_all_placeholders(data)

    # Step 4: Validate required global keys
    errors = _validate_required_global_keys(data)

    # Step 5: Validate per-client entries
    errors.extend(_validate_clients(data))

    if errors:
        error_msg = "Configuration validation failed:\n" + "\n".join(
            f"  - {e}" for e in errors
        )
        logger.error(error_msg)
        raise ConfigError(error_msg)

    # Step 6: Construct AppConfig
    # Parse alert defaults
    alert_defaults_data = data.get("alert_defaults", {})
    # Map the YAML key name to model field name if needed
    if "consecutive_negative_sentiment" in alert_defaults_data:
        alert_defaults_data["consecutive_neg_sentiment"] = alert_defaults_data.pop(
            "consecutive_negative_sentiment"
        )
    alert_defaults = AlertThresholds(**alert_defaults_data)

    # Parse clients
    clients: dict[str, ClientConfig] = {}
    for client_data in data.get("clients", []):
        client_config = _parse_client_config(client_data, alert_defaults)
        clients[client_config.client_id] = client_config

    # Build AppConfig
    config = AppConfig(
        webhook_secret=data["webhook_secret"],
        nim_api_key=data["nim"]["api_key"],
        nim_base_url=data["nim"]["base_url"],
        nim_model=data["nim"]["model"],
        telegram_bot_token=data["telegram"]["bot_token"],
        telegram_chat_id=str(data["telegram"]["chat_id"]),
        digest_schedule=data["digest"]["schedule"],
        alert_defaults=alert_defaults,
        clients=clients,
        db_path=data.get("db_path", "data/monitor.db"),
    )

    logger.info(
        "Configuration loaded successfully with %d client(s)", len(clients)
    )
    return config
