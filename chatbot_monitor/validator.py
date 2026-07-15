"""Payload validation, field checking, and chat history truncation.

This module validates incoming webhook payloads against the required schema,
truncates chat_history to the maximum allowed messages, and extracts optional fields.

Supports both the canonical format (contact_id, timestamp, chat_history) and
Chatrace's "All Contact Data" format, which sends contact fields at the top level
with chat history stored in a custom_fields entry named "chat history".
"""

import logging
import re
from datetime import datetime, timezone

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


def _parse_chatrace_chat_history(text: str) -> list[dict]:
    """Parse Chatrace's plain-text chat history into structured messages.

    Chatrace stores chat history as a custom field with format:
        User (2026-07-9 2:56pm): hi
        I (2026-07-9 2:56pm): hey, welcome!

    Args:
        text: The raw chat history text from Chatrace.

    Returns:
        List of message dicts with 'role', 'content', and optionally 'timestamp'.
    """
    messages = []
    # Pattern: "User (...): message" or "I (...): message"
    # Split on lines that start with "User (" or "I ("
    pattern = re.compile(
        r'^(User|I)\s*\(([^)]+)\):\s*(.*?)(?=\n(?:User|I)\s*\(|$)',
        re.MULTILINE | re.DOTALL
    )

    for match in pattern.finditer(text):
        sender = match.group(1)
        timestamp_str = match.group(2).strip()
        content = match.group(3).strip()

        if not content:
            continue

        role = "user" if sender == "User" else "bot"
        messages.append({
            "role": role,
            "content": content,
            "timestamp": timestamp_str,
        })

    return messages


def _transform_chatrace_payload(body: dict) -> dict:
    """Transform Chatrace's 'All Contact Data' format into our canonical format.

    Chatrace sends:
    - id / phone: contact identifier
    - last_interaction: unix timestamp in milliseconds
    - custom_fields: array with a "chat history" entry containing the transcript
    - tags: list of tag strings

    We transform to:
    - contact_id: from phone or id
    - timestamp: ISO 8601 from last_interaction
    - chat_history: parsed from custom_fields "chat history" value
    - tags: passed through

    Args:
        body: The raw Chatrace payload.

    Returns:
        Transformed dict in canonical format, or the original body if it
        doesn't look like a Chatrace payload.
    """
    # Detect if this is a Chatrace payload (has 'custom_fields' or 'page_id')
    if "custom_fields" not in body and "page_id" not in body:
        return body  # Not a Chatrace payload, return as-is

    logger.info("Detected Chatrace payload format, transforming")

    transformed = {}

    # Extract contact_id from phone or id
    transformed["contact_id"] = body.get("phone") or body.get("id") or ""

    # Store the Chatrace numeric ID separately for API callbacks
    # The Chatrace API requires the numeric ID for endpoints like tagging
    transformed["chatrace_id"] = body.get("id") or ""

    # Extract timestamp from last_interaction (unix ms) or created_at
    last_interaction = body.get("last_interaction")
    if last_interaction:
        try:
            ts_seconds = int(last_interaction) / 1000.0
            dt = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
            transformed["timestamp"] = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError, OSError):
            # Fallback to created_at if available
            created_at = body.get("created_at")
            if created_at:
                transformed["timestamp"] = created_at.replace(" ", "T") + "Z"
            else:
                transformed["timestamp"] = ""
    else:
        created_at = body.get("created_at")
        if created_at:
            transformed["timestamp"] = created_at.replace(" ", "T") + "Z"
        else:
            transformed["timestamp"] = ""

    # Extract chat_history from custom_fields
    chat_history_text = ""
    custom_fields = body.get("custom_fields", [])
    if isinstance(custom_fields, list):
        for field in custom_fields:
            if isinstance(field, dict):
                field_name = field.get("name", "").lower().strip()
                if field_name == "chat history":
                    chat_history_text = field.get("value", "")
                    break

    if chat_history_text:
        parsed_messages = _parse_chatrace_chat_history(chat_history_text)
        transformed["chat_history"] = parsed_messages
    else:
        transformed["chat_history"] = []

    # Pass through tags
    transformed["tags"] = body.get("tags", [])

    # Extract user_source from channel or other fields
    channel = body.get("channel")
    if channel:
        transformed["user_source"] = f"channel_{channel}"

    return transformed


def validate_payload(client_id: str, body: dict) -> WebhookPayload:
    """Validate required fields, truncate chat_history, and extract optional fields.

    Supports both canonical format and Chatrace's 'All Contact Data' format.
    If a Chatrace payload is detected, it is automatically transformed before
    validation.

    Args:
        client_id: The client identifier from the URL path.
        body: The raw request body as a dictionary.

    Returns:
        A validated WebhookPayload with truncated chat_history and extracted fields.

    Raises:
        ValidationError: If required fields are missing, chat_history is empty,
                        or timestamp format is invalid.
    """
    # Transform Chatrace payload if detected
    body = _transform_chatrace_payload(body)

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

    # Normalize tags — Chatrace sends them as dicts like {'id': '771401'}
    raw_tags = body.get("tags")
    normalized_tags = None
    if raw_tags and isinstance(raw_tags, list):
        normalized_tags = []
        for tag in raw_tags:
            if isinstance(tag, str):
                normalized_tags.append(tag)
            elif isinstance(tag, dict):
                # Extract tag name or id
                normalized_tags.append(tag.get("name", tag.get("id", str(tag))))

    # Build the validated payload with optional fields
    payload = WebhookPayload(
        contact_id=body["contact_id"],
        timestamp=body["timestamp"],
        chat_history=chat_messages,
        tags=normalized_tags,
        last_ref=body.get("last_ref"),
        user_source=body.get("user_source"),
        chatrace_id=body.get("chatrace_id"),
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
