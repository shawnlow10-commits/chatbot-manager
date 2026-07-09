"""Deduplication key computation using SHA-256 hashing."""

import hashlib
from datetime import datetime, timezone


def compute_dedupe_key(client_id: str, contact_id: str, timestamp: str) -> str:
    """SHA-256 hash of client_id + contact_id + timestamp (normalized to second precision).

    Args:
        client_id: The client/bot identifier.
        contact_id: The contact identifier from the payload.
        timestamp: ISO 8601 timestamp string (may include sub-second precision).

    Returns:
        Hex digest of the SHA-256 hash of the concatenated normalized inputs.

    Raises:
        ValueError: If the timestamp cannot be parsed as ISO 8601.
    """
    normalized_ts = _normalize_timestamp(timestamp)
    combined = client_id + contact_id + normalized_ts
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def _normalize_timestamp(timestamp: str) -> str:
    """Normalize a timestamp to second precision in ISO 8601 format.

    Strips sub-second components so that timestamps differing only in
    microseconds produce the same dedupe key.

    Examples:
        "2024-01-15T10:30:00.123456Z" → "2024-01-15T10:30:00Z"
        "2024-01-15T10:30:00Z" → "2024-01-15T10:30:00Z"
        "2024-01-15T10:30:00+03:00" → "2024-01-15T10:30:00+03:00"

    Args:
        timestamp: ISO 8601 timestamp string.

    Returns:
        Timestamp string normalized to second precision.

    Raises:
        ValueError: If the timestamp cannot be parsed.
    """
    # Handle 'Z' suffix by replacing with +00:00 for fromisoformat compatibility
    ts_input = timestamp
    if ts_input.endswith("Z"):
        ts_input = ts_input[:-1] + "+00:00"

    dt = datetime.fromisoformat(ts_input)

    # Replace microsecond with 0 to strip sub-second precision
    dt = dt.replace(microsecond=0)

    # Format back to ISO 8601
    if dt.tzinfo is not None and dt.utcoffset() is not None:
        if dt.utcoffset().total_seconds() == 0:
            # Use 'Z' suffix for UTC
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            return dt.isoformat()
    else:
        # Naive datetime (no timezone)
        return dt.isoformat()
