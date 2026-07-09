"""Unit tests for the deduplicator module."""

import hashlib

import pytest

from chatbot_monitor.deduplicator import compute_dedupe_key, _normalize_timestamp


class TestNormalizeTimestamp:
    """Tests for timestamp normalization to second precision."""

    def test_strips_microseconds_utc(self):
        result = _normalize_timestamp("2024-01-15T10:30:00.123456Z")
        assert result == "2024-01-15T10:30:00Z"

    def test_preserves_already_normalized_utc(self):
        result = _normalize_timestamp("2024-01-15T10:30:00Z")
        assert result == "2024-01-15T10:30:00Z"

    def test_strips_milliseconds_utc(self):
        result = _normalize_timestamp("2024-01-15T10:30:00.123Z")
        assert result == "2024-01-15T10:30:00Z"

    def test_preserves_non_utc_offset(self):
        result = _normalize_timestamp("2024-01-15T10:30:00+03:00")
        assert result == "2024-01-15T10:30:00+03:00"

    def test_strips_microseconds_with_offset(self):
        result = _normalize_timestamp("2024-01-15T10:30:00.999999+05:30")
        assert result == "2024-01-15T10:30:00+05:30"

    def test_naive_timestamp(self):
        result = _normalize_timestamp("2024-01-15T10:30:00")
        assert result == "2024-01-15T10:30:00"

    def test_naive_timestamp_with_microseconds(self):
        result = _normalize_timestamp("2024-01-15T10:30:00.500000")
        assert result == "2024-01-15T10:30:00"

    def test_invalid_timestamp_raises_value_error(self):
        with pytest.raises(ValueError):
            _normalize_timestamp("not-a-timestamp")

    def test_empty_string_raises_value_error(self):
        with pytest.raises(ValueError):
            _normalize_timestamp("")


class TestComputeDedupeKey:
    """Tests for SHA-256 dedupe key computation."""

    def test_returns_sha256_hex_digest(self):
        result = compute_dedupe_key("client1", "contact1", "2024-01-15T10:30:00Z")
        # Manually compute expected
        expected = hashlib.sha256(
            "client1contact12024-01-15T10:30:00Z".encode("utf-8")
        ).hexdigest()
        assert result == expected

    def test_deterministic_same_inputs(self):
        key1 = compute_dedupe_key("bot_a", "user_123", "2024-06-01T12:00:00Z")
        key2 = compute_dedupe_key("bot_a", "user_123", "2024-06-01T12:00:00Z")
        assert key1 == key2

    def test_sub_second_precision_ignored(self):
        key_with_ms = compute_dedupe_key(
            "bot_a", "user_123", "2024-06-01T12:00:00.123456Z"
        )
        key_without_ms = compute_dedupe_key(
            "bot_a", "user_123", "2024-06-01T12:00:00Z"
        )
        assert key_with_ms == key_without_ms

    def test_different_sub_second_same_key(self):
        key1 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:00.111Z")
        key2 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:00.999Z")
        assert key1 == key2

    def test_different_client_id_different_key(self):
        key1 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:00Z")
        key2 = compute_dedupe_key("bot_b", "user_1", "2024-01-01T00:00:00Z")
        assert key1 != key2

    def test_different_contact_id_different_key(self):
        key1 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:00Z")
        key2 = compute_dedupe_key("bot_a", "user_2", "2024-01-01T00:00:00Z")
        assert key1 != key2

    def test_different_timestamp_different_key(self):
        key1 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:00Z")
        key2 = compute_dedupe_key("bot_a", "user_1", "2024-01-01T00:00:01Z")
        assert key1 != key2

    def test_returns_64_char_hex_string(self):
        result = compute_dedupe_key("x", "y", "2024-01-01T00:00:00Z")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_invalid_timestamp_raises_value_error(self):
        with pytest.raises(ValueError):
            compute_dedupe_key("client", "contact", "invalid-ts")
