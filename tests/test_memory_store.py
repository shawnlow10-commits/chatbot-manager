"""Unit tests for the MemoryStore module."""

import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone

from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import (
    StructuredOutput,
    Outcome,
    DropOffStage,
    Sentiment,
)


@pytest_asyncio.fixture
async def store():
    """Create an in-memory MemoryStore for testing."""
    s = MemoryStore(":memory:")
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_initialize_creates_tables(store: MemoryStore):
    """Verify that initialize creates all expected tables."""
    assert store._db is not None
    cursor = await store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    rows = await cursor.fetchall()
    table_names = sorted([row[0] for row in rows])
    assert "dedupe_keys" in table_names
    assert "raw_payloads" in table_names
    assert "structured_outputs" in table_names
    assert "flag_history" in table_names
    assert "digest_log" in table_names


@pytest.mark.asyncio
async def test_has_dedupe_key_returns_false_for_missing(store: MemoryStore):
    """has_dedupe_key returns False when key doesn't exist."""
    result = await store.has_dedupe_key("nonexistent_key")
    assert result is False


@pytest.mark.asyncio
async def test_store_and_check_dedupe_key(store: MemoryStore):
    """Store a dedupe key and verify it can be found."""
    await store.store_dedupe_key("test_key_123", "client_a")
    result = await store.has_dedupe_key("test_key_123")
    assert result is True


@pytest.mark.asyncio
async def test_store_dedupe_key_idempotent(store: MemoryStore):
    """Storing the same dedupe key twice doesn't raise."""
    await store.store_dedupe_key("dup_key", "client_a")
    await store.store_dedupe_key("dup_key", "client_a")  # Should not error
    result = await store.has_dedupe_key("dup_key")
    assert result is True


@pytest.mark.asyncio
async def test_store_raw_payload(store: MemoryStore):
    """Store a raw payload and verify it's persisted."""
    await store.store_dedupe_key("raw_key_1", "client_b")
    await store.store_raw_payload(
        client_id="client_b",
        dedupe_key="raw_key_1",
        contact_id="contact_1",
        timestamp="2024-01-15T10:30:00Z",
        payload={"chat_history": [{"role": "user", "content": "Hello"}]},
    )
    # Verify by querying directly
    cursor = await store._db.execute(
        "SELECT client_id, contact_id, timestamp FROM raw_payloads WHERE dedupe_key = ?",
        ("raw_key_1",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "client_b"
    assert row[1] == "contact_1"
    assert row[2] == "2024-01-15T10:30:00Z"


@pytest.mark.asyncio
async def test_store_structured_output(store: MemoryStore):
    """Store a structured output and verify fields are persisted correctly."""
    await store.store_dedupe_key("so_key_1", "client_c")
    output = StructuredOutput(
        outcome=Outcome.QUALIFIED_LEAD,
        drop_off_stage=None,
        sentiment=Sentiment.POSITIVE,
        bot_error_detected=False,
        bot_error_notes=None,
        notable_quote="Great service!",
        summary="Lead was qualified successfully.",
    )
    await store.store_structured_output(
        client_id="client_c",
        contact_id="contact_2",
        dedupe_key="so_key_1",
        timestamp="2024-01-15T11:00:00Z",
        output=output,
    )
    # Verify by querying directly
    cursor = await store._db.execute(
        "SELECT outcome, sentiment, bot_error_detected, summary FROM structured_outputs WHERE dedupe_key = ?",
        ("so_key_1",),
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "qualified_lead"
    assert row[1] == "positive"
    assert row[2] == 0
    assert row[3] == "Lead was qualified successfully."


@pytest.mark.asyncio
async def test_get_rolling_aggregates_empty(store: MemoryStore):
    """Rolling aggregates return empty values for a client with no data."""
    agg = await store.get_rolling_aggregates("empty_client")
    assert agg.total_conversations_7d == 0
    assert agg.total_conversations_30d == 0
    assert agg.daily_volume_7d == []
    assert agg.outcome_dist_7d == {}
    assert agg.recent_errors == []


@pytest.mark.asyncio
async def test_get_rolling_aggregates_with_data(store: MemoryStore):
    """Rolling aggregates correctly compute from stored structured outputs."""
    now = datetime.now(timezone.utc)
    client_id = "client_agg"

    # Insert 5 conversations within last 7 days
    for i in range(5):
        ts = (now - timedelta(days=i % 3, hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = f"agg_key_{i}"
        await store.store_dedupe_key(key, client_id)
        output = StructuredOutput(
            outcome=Outcome.DROPPED_OFF if i < 2 else Outcome.QUALIFIED_LEAD,
            drop_off_stage=DropOffStage.GREETING if i < 2 else None,
            sentiment=Sentiment.NEGATIVE if i == 0 else Sentiment.NEUTRAL,
            bot_error_detected=(i == 1),
            summary=f"Conversation {i}",
        )
        await store.store_structured_output(
            client_id=client_id,
            contact_id=f"contact_{i}",
            dedupe_key=key,
            timestamp=ts,
            output=output,
        )

    agg = await store.get_rolling_aggregates(client_id)
    assert agg.total_conversations_7d == 5
    assert sum(agg.daily_volume_7d) == 5
    assert sum(agg.outcome_dist_7d.values()) == 5
    assert agg.outcome_dist_7d.get("dropped_off", 0) == 2
    assert agg.outcome_dist_7d.get("qualified_lead", 0) == 3
    assert sum(agg.dropoff_by_stage_7d.values()) == 2
    assert agg.dropoff_by_stage_7d.get("greeting", 0) == 2
    assert len(agg.recent_errors) == 5
    assert agg.recent_errors[0] is False  # most recent (i=4) has no error


@pytest.mark.asyncio
async def test_get_conversations_since(store: MemoryStore):
    """get_conversations_since returns correct subset of conversations."""
    client_id = "client_since"
    now = datetime.now(timezone.utc)

    # Insert conversations at different times
    for i in range(4):
        ts = (now - timedelta(hours=i * 12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        key = f"since_key_{i}"
        await store.store_dedupe_key(key, client_id)
        output = StructuredOutput(
            outcome=Outcome.BOOKED,
            sentiment=Sentiment.POSITIVE,
            bot_error_detected=False,
            summary=f"Conv {i}",
        )
        await store.store_structured_output(
            client_id=client_id,
            contact_id=f"contact_{i}",
            dedupe_key=key,
            timestamp=ts,
            output=output,
        )

    # Get conversations from last 24 hours
    since = now - timedelta(hours=24)
    results = await store.get_conversations_since(client_id, since)
    # Should include conversations at 0h, 12h, and 24h ago
    assert len(results) >= 2


@pytest.mark.asyncio
async def test_get_last_conversation_time(store: MemoryStore):
    """get_last_conversation_time returns the most recent timestamp."""
    client_id = "client_last"
    now = datetime.now(timezone.utc)

    # No conversations yet
    result = await store.get_last_conversation_time(client_id)
    assert result is None

    # Add a conversation
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    await store.store_dedupe_key("last_key", client_id)
    output = StructuredOutput(
        outcome=Outcome.UNCLEAR,
        sentiment=Sentiment.NEUTRAL,
        bot_error_detected=False,
        summary="Test conversation",
    )
    await store.store_structured_output(
        client_id=client_id,
        contact_id="contact_x",
        dedupe_key="last_key",
        timestamp=ts,
        output=output,
    )

    result = await store.get_last_conversation_time(client_id)
    assert result is not None
    # Should be close to now
    assert abs((result - now).total_seconds()) < 2


@pytest.mark.asyncio
async def test_record_flag_and_cooldown(store: MemoryStore):
    """record_flag creates a cooldown and is_in_cooldown detects it."""
    client_id = "client_flag"
    issue_type = "high_dropoff"
    stage = "greeting"

    # Initially not in cooldown
    in_cooldown = await store.is_in_cooldown(client_id, issue_type, stage)
    assert in_cooldown is False

    # Record a flag with 60 min cooldown
    await store.record_flag(
        client_id=client_id,
        issue_type=issue_type,
        stage=stage,
        metric=75.0,
        baseline=50.0,
        cooldown_minutes=60,
    )

    # Now should be in cooldown
    in_cooldown = await store.is_in_cooldown(client_id, issue_type, stage)
    assert in_cooldown is True


@pytest.mark.asyncio
async def test_cooldown_stage_none(store: MemoryStore):
    """Cooldown works correctly when stage is None."""
    client_id = "client_null_stage"
    issue_type = "consecutive_errors"

    # Not in cooldown initially
    assert await store.is_in_cooldown(client_id, issue_type, None) is False

    # Record flag with None stage
    await store.record_flag(
        client_id=client_id,
        issue_type=issue_type,
        stage=None,
        metric=3.0,
        baseline=0.0,
        cooldown_minutes=30,
    )

    # Should be in cooldown
    assert await store.is_in_cooldown(client_id, issue_type, None) is True

    # Different issue_type should NOT be in cooldown
    assert await store.is_in_cooldown(client_id, "different_type", None) is False


@pytest.mark.asyncio
async def test_purge_old_records(store: MemoryStore):
    """purge_old_records removes records older than retention periods."""
    client_id = "client_purge"

    # Insert a dedupe key with an old created_at
    old_time = (datetime.now(timezone.utc) - timedelta(days=35)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    await store._db.execute(
        "INSERT INTO dedupe_keys (key, client_id, created_at) VALUES (?, ?, ?)",
        ("old_key", client_id, old_time),
    )
    await store._db.commit()

    # Insert a recent dedupe key
    await store.store_dedupe_key("recent_key", client_id)

    # Purge with 30-day dedupe retention
    purged = await store.purge_old_records(dedupe_days=30, data_days=90)

    # Old key should be gone
    assert await store.has_dedupe_key("old_key") is False
    # Recent key should still exist
    assert await store.has_dedupe_key("recent_key") is True
    assert purged >= 1


@pytest.mark.asyncio
async def test_close_and_reopen(store: MemoryStore):
    """Verify store can be closed without errors."""
    await store.close()
    assert store._db is None
