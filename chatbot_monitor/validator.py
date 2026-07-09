"""Payload validation, field checking, and chat history truncation.

This module validates incoming webhook payloads against the required schema,
truncates chat_history to the maximum allowed messages, and extracts optional fields.
"""

import logging
from datetime import datetime

from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.models import ChatMessage, WebhookPayload

logger = get_logger("validator")

# Maximum number of messages allowed in chat_history
MAX_CHAT_MESSAGES = 50


class ValidationError(Exception):
    """Raised when a webhook payload fails validation.

    Attributes:
        errors: List of human-readable error descriptions identifying
                the specific validation failures.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Payload validation failed: {'; '.join(errors)}")


def truncate_chat_history(
    messages: list[dict], max_messages: int = MAX_CHAT_MESSAGES
) -> list[dict]:
    """Return the last `max_messages` messages from the list, preserving order.

    If the list has fewer than or equal to max_messages entries, it is returned
    unchanged. Otherwise, only the last max_messages entries are kept.

    Args:
        messages: List of message dictionaries from the payload.
        max_messages: Maximum number of messages to retain. Defaults to 50.

    Returns:
        A list of at most max_messages message dicts, taken from the end of the input.
    """
    if len(messages) <= max_messages:
        return messages
    return messages[-max_messages:]


def _validate_timestamp(timestamp: str) -> bool:
    """Check if timestamp is valid ISO 8601 format.

    Attempts several common ISO 8601 formats to determine validity.

    Args:
        timestamp: The timestamp string to validate.

    Returns:
        True if the timestamp is valid ISO 8601, False otherwise.
    """
    try:
        # Try Python's fromisoformat which handles most ISO 8601 variants
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return True
    except (ValueError, AttributeError):
        return False


def validate_payload(client_id: str, body: dict) -> WebhookPayload:
    """Validate required fields, truncate chat_history, and extract optional fields.

    Validates that the incoming payload contains all required fields with correct
    types and formats. Truncates chat_history to the last 50 messages if it exceeds
    the limit. Extracts optional fields (tags, last_ref, user_source) when present.

    Args:
        client_id: The client identifier from the URL path.
        body: The raw request body as a dictionary.

    Returns:
        A validated WebhookPayload with truncated chat_history and extracted fields.

    Raises:
        ValidationError: If required fields are missing, chat_history is empty,
                        or timestamp format is invalid.
    """
    errors: list[str] = []

    # Check for required fields
    if "chat_history" not in body:
        errors.append("Missing required field: chat_history")
    if "contact_id" not in body:
        errors.append("Missing required field: contact_id")
    if "timestamp" not in body:
        errors.append("Missing required field: timestamp")

    # If any required fields are missing, raise immediately
    if errors:
        logger.warning(
            "Payload validation failed for client %s: %s",
            client_id,
            "; ".join(errors),
            extra={"client_id": client_id, "validation_errors": errors},
        )
        raise ValidationError(errors)

    # Validate contact_id is a string
    if not isinstance(body["contact_id"], str) or not body["contact_id"].strip():
        errors.append("Field 'contact_id' must be a non-empty string")

    # Validate chat_history is a non-empty list
    chat_history = body["chat_history"]
    if not isinstance(chat_history, list):
        errors.append("Field 'chat_history' must be a list")
    elif len(chat_history) == 0:
        errors.append("Field 'chat_history' must not be empty")

    # Validate timestamp is valid ISO 8601
    timestamp = body["timestamp"]
    if not isinstance(timestamp, str):
        errors.append("Field 'timestamp' must be a string")
    elif not _validate_timestamp(timestamp):
        errors.append(
            "Field 'timestamp' is not valid ISO 8601 format"
        )

    # Raise if there are validation errors
    if errors:
        logger.warning(
            "Payload validation failed for client %s: %s",
            client_id,
            "; ".join(errors),
            extra={"client_id": client_id, "validation_errors": errors},
        )
        raise ValidationError(errors)

    # Truncate chat_history to last 50 messages
    truncated_history = truncate_chat_history(chat_history)

    if len(chat_history) > MAX_CHAT_MESSAGES:
        logger.info(
            "Truncated chat_history from %d to %d messages for client %s",
            len(chat_history),
            MAX_CHAT_MESSAGES,
            client_id,
            extra={"client_id": client_id, "original_count": len(chat_history)},
        )

    # Convert message dicts to ChatMessage objects
    chat_messages = [ChatMessage(**msg) for msg in truncated_history]

    # Build the validated payload with optional fields
    payload = WebhookPayload(
        contact_id=body["contact_id"],
        timestamp=body["timestamp"],
        chat_history=chat_messages,
        tags=body.get("tags"),
        last_ref=body.get("last_ref"),
        user_source=body.get("user_source"),
    )

    logger.debug(
        "Payload validated successfully for client %s, contact %s",
        client_id,
        body["contact_id"],
        extra={
            "client_id": client_id,
            "contact_id": body["contact_id"],
            "message_count": len(chat_messages),
        },
    )

    return payload
