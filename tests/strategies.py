"""Hypothesis strategies for property-based testing.

Provides reusable strategies for generating valid domain objects
used across all property-based tests in the chatbot monitor suite.
"""

from datetime import datetime

from hypothesis import strategies as st

from chatbot_monitor.models import (
    ActiveHours,
    AlertThresholds,
    AnomalyAlert,
    DigestMessage,
    DigestSection,
    DropOffStage,
    Outcome,
    Sentiment,
    StructuredOutput,
)

# ─── Primitive Strategies ─────────────────────────────────────────────────────

valid_client_ids = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), whitelist_characters="_-"),
    min_size=1,
    max_size=50,
)

valid_contact_ids = st.text(min_size=1, max_size=100)

valid_timestamps = st.datetimes(
    min_value=datetime(2020, 1, 1),
    max_value=datetime(2030, 12, 31),
).map(lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ"))


# ─── Chat Message Strategies ─────────────────────────────────────────────────

chat_messages = st.fixed_dictionaries(
    {
        "role": st.sampled_from(["user", "bot"]),
        "content": st.text(min_size=1, max_size=500),
    }
)

chat_histories = st.lists(chat_messages, min_size=1, max_size=200)


# ─── Structured Output Strategies ────────────────────────────────────────────

structured_outputs = st.builds(
    StructuredOutput,
    outcome=st.sampled_from(Outcome),
    drop_off_stage=st.one_of(st.none(), st.sampled_from(DropOffStage)),
    sentiment=st.sampled_from(Sentiment),
    bot_error_detected=st.booleans(),
    bot_error_notes=st.one_of(st.none(), st.text(max_size=500)),
    notable_quote=st.one_of(st.none(), st.text(max_size=300)),
    summary=st.text(min_size=1, max_size=200),
)


# ─── Alert Strategies ─────────────────────────────────────────────────────────

anomaly_alerts = st.builds(
    AnomalyAlert,
    client_id=valid_client_ids,
    client_display_name=st.text(min_size=1, max_size=100),
    issue_type=st.sampled_from(
        [
            "high_dropoff",
            "consecutive_errors",
            "low_volume",
            "negative_sentiment",
            "inactive_hours",
        ]
    ),
    stage=st.one_of(st.none(), st.sampled_from(DropOffStage).map(lambda s: s.value)),
    metric_value=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    baseline_value=st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    message=st.text(min_size=1, max_size=500),
)


# ─── Digest Strategies ────────────────────────────────────────────────────────

digest_sections = st.builds(
    DigestSection,
    client_id=valid_client_ids,
    client_display_name=st.text(min_size=1, max_size=100),
    bullets=st.lists(st.text(min_size=1, max_size=280), min_size=1, max_size=10),
)

digest_messages = st.builds(
    DigestMessage,
    sections=st.lists(digest_sections, min_size=1, max_size=5),
    generated_at=st.datetimes(
        min_value=datetime(2020, 1, 1),
        max_value=datetime(2030, 12, 31),
    ),
)


# ─── Configuration Strategies ─────────────────────────────────────────────────

config_dicts = st.fixed_dictionaries(
    {
        "webhook_secret": st.text(min_size=1, max_size=100),
        "nim": st.fixed_dictionaries(
            {
                "api_key": st.text(min_size=1, max_size=100),
                "base_url": st.just("https://integrate.api.nvidia.com/v1"),
                "model": st.text(min_size=1, max_size=100),
            }
        ),
        "telegram": st.fixed_dictionaries(
            {
                "bot_token": st.text(min_size=1, max_size=100),
                "chat_id": st.text(min_size=1, max_size=50),
            }
        ),
        "digest": st.fixed_dictionaries(
            {
                "schedule": st.just("0 8 * * *"),
            }
        ),
        "alert_defaults": st.fixed_dictionaries(
            {
                "dropoff_rate_pct": st.floats(min_value=1.0, max_value=100.0, allow_nan=False, allow_infinity=False),
                "low_volume_pct": st.floats(min_value=1.0, max_value=100.0, allow_nan=False, allow_infinity=False),
                "consecutive_errors": st.integers(min_value=1, max_value=50),
                "consecutive_neg_sentiment": st.integers(min_value=1, max_value=50),
                "persistence_count": st.integers(min_value=1, max_value=50),
                "cooldown_minutes": st.integers(min_value=1, max_value=1440),
            }
        ),
        "db_path": st.just("data/monitor.db"),
        "clients": st.lists(
            st.fixed_dictionaries(
                {
                    "client_id": valid_client_ids,
                    "display_name": st.text(min_size=1, max_size=100),
                    "thresholds": st.just({}),
                }
            ),
            min_size=0,
            max_size=3,
        ),
    }
)
