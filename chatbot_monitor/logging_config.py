"""Structured JSON logging configuration with field truncation and contact tracing.

All log entries are output to stdout as valid JSON objects containing:
- timestamp: ISO 8601 format
- level: log level (INFO, DEBUG, ERROR, etc.)
- module: the logger name / component
- message: the log message
- contact_id: included when processing a specific contact (via contextvars)

Any individual field value exceeding 10,000 characters is truncated with an
ellipsis marker to prevent log bloat.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# Context variable for tracing contact_id across async request processing
contact_id_var: ContextVar[str | None] = ContextVar("contact_id", default=None)

# Maximum length for any single field value in a log entry
MAX_FIELD_LENGTH = 10_000
TRUNCATION_SUFFIX = "...[TRUNCATED]"


def truncate_field(value: Any) -> Any:
    """Truncate a field value if it exceeds MAX_FIELD_LENGTH characters.

    Only string values are truncated. Other types are returned as-is.
    Truncated strings end with an ellipsis marker indicating truncation occurred.
    """
    if isinstance(value, str) and len(value) > MAX_FIELD_LENGTH:
        return value[: MAX_FIELD_LENGTH - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX
    return value


class JSONFormatter(logging.Formatter):
    """Custom log formatter that outputs each record as a single JSON line.

    Output format:
        {"timestamp": "...", "level": "...", "module": "...", "message": "...", ...}

    Extra fields from the log record's `extra` dict are included in the output.
    The contact_id is automatically pulled from the context variable when available.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Include contact_id from context variable if set
        current_contact_id = contact_id_var.get()
        if current_contact_id is not None:
            log_entry["contact_id"] = current_contact_id

        # Include any extra fields passed via the `extra` kwarg
        # Standard LogRecord attributes to exclude from extras
        standard_attrs = {
            "name", "msg", "args", "created", "relativeCreated",
            "thread", "threadName", "msecs", "filename", "funcName",
            "levelno", "lineno", "module", "exc_info", "exc_text",
            "stack_info", "pathname", "processName", "process",
            "message", "levelname", "taskName",
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs and not key.startswith("_"):
                log_entry[key] = value

        # Apply truncation to all field values
        log_entry = {k: truncate_field(v) for k, v in log_entry.items()}

        return json.dumps(log_entry, default=str, ensure_ascii=False)


def setup_logging(level: str = "INFO") -> None:
    """Configure the root logger with structured JSON output to stdout.

    Args:
        level: The logging level string (e.g., "INFO", "DEBUG", "ERROR").
               Defaults to "INFO".
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove any existing handlers to avoid duplicate output
    root_logger.handlers.clear()

    # Create stdout handler with JSON formatter
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance for the given module name.

    Args:
        name: The module/component name (e.g., "receiver", "nim_analyzer").

    Returns:
        A configured Logger instance.
    """
    return logging.getLogger(name)
