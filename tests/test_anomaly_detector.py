"""Unit tests for the anomaly detector module."""

import pytest
from datetime import datetime, timezone, time
from unittest.mock import AsyncMock, MagicMock, patch

from chatbot_monitor.anomaly_detector import AnomalyDetector
from chatbot_monitor.config import AppConfig, ClientConfig
from chatbot_monitor.models import (
    ActiveHours,
    AlertThresholds,
    AnomalyAlert,
    RollingAggregates,
    StructuredOutput,
    Outcome,
    DropOffStage,
    Sentiment,
)


@pytest.fixture
def alert_defaults():
    return AlertThresholds(
        dropoff_rate_pct=50.0,
        low_volume_pct=50.0,
        consecutive_errors=3,
        consecutive_neg_sentiment=3,
        persistence_count=3,
        cooldown_minutes=60,
    )


@pytest.fixture
def client_config(alert_defaults):
    return ClientConfig(
        client_id="bot_test",
        display_name="Test Bot",
        thresholds=alert_defaults,
        active_hours=ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],
        ),
    )


@pytest.fixture
def app_config(alert_defaults, client_config):
    return AppConfig(
        webhook_secret="test-secret",
        nim_api_key="test-key",
        nim_base_url="https://nim.example.com",
        nim_model="test-model",
        telegram_bot_token="bot-token",
        telegram_chat_id="chat-id",
        digest_schedule="0 8 * * *",
        alert_defaults=alert_defaults,
        clients={"bot_test": client_config},
        db_path=":memory:",
    )


@pytest.fixture
def mock_store():
    store = AsyncMock()
    store.is_in_cooldown = AsyncMock(return_value=False)
    store.record_flag = AsyncMock()
    return store


@pytest.fixture
def mock_notifier():
    notifier = AsyncMock()
    notifier.send_alert = AsyncMock(return_value=True)
    return notifier


@pytest.fixture
def detector(app_config, mock_store, mock_notifier):
    return AnomalyDetector(app_config, mock_store, mock_notifier)


def make_output(
    outcome="qualified_lead",
    drop_off_stage=None,
    sentiment="neutral",
    bot_error_detected=False,
):
    return StructuredOutput(
        outcome=outcome,
        drop_off_stage=drop_off_stage,
        sentiment=sentiment,
        bot_error_detected=bot_error_detected,
        bot_error_notes=None,
        notable_quote=None,
        summary="Test conversation",
    )


def make_aggregates(
    total_7d=20,
    daily_volume_7d=None,
    dropoff_by_stage_7d=None,
    recent_errors=None,
    recent_sentiments=None,
):
    return RollingAggregates(
        daily_volume_7d=daily_volume_7d or [3, 3, 3, 3, 3, 3, 2],
        daily_volume_30d=[3] * 30,
        outcome_dist_7d={"qualified_lead": 15, "dropped_off": 5},
        outcome_dist_30d={"qualified_lead": 60, "dropped_off": 20, "booked": 10},
        dropoff_by_stage_7d=dropoff_by_stage_7d or {"greeting": 2, "qualification": 3},
        dropoff_by_stage_30d={"greeting": 8, "qualification": 12},
        sentiment_dist_7d={"positive": 10, "neutral": 8, "negative": 2},
        sentiment_dist_30d={"positive": 40, "neutral": 35, "negative": 15},
        recent_errors=recent_errors or [False, False, False, False, False],
        recent_sentiments=recent_sentiments
        or ["positive", "neutral", "positive", "neutral", "positive"],
        total_conversations_7d=total_7d,
        total_conversations_30d=90,
    )


class TestEvaluate:
    """Tests for the evaluate() method."""

    async def test_insufficient_data_skips_evaluation(self, detector, mock_store):
        """Skip evaluation when total conversations < persistence_count."""
        aggregates = make_aggregates(total_7d=2)
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output()

        alerts = await detector.evaluate("bot_test", output)

        assert alerts == []

    async def test_no_anomalies_for_normal_conversation(self, detector, mock_store):
        """No alerts for a normal conversation within baselines."""
        aggregates = make_aggregates()
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output()

        alerts = await detector.evaluate("bot_test", output)

        assert alerts == []

    async def test_consecutive_errors_triggers_alert(
        self, detector, mock_store, mock_notifier
    ):
        """Alert when N consecutive conversations have bot errors."""
        aggregates = make_aggregates(
            recent_errors=[True, True, True, False, False]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(bot_error_detected=True)

        alerts = await detector.evaluate("bot_test", output)

        assert len(alerts) == 1
        assert alerts[0].issue_type == "consecutive_errors"
        assert alerts[0].client_id == "bot_test"
        mock_notifier.send_alert.assert_called()

    async def test_consecutive_errors_not_triggered_below_threshold(
        self, detector, mock_store
    ):
        """No alert when errors are below consecutive threshold."""
        aggregates = make_aggregates(
            recent_errors=[True, True, False, False, False]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(bot_error_detected=True)

        alerts = await detector.evaluate("bot_test", output)

        # Should not have a consecutive_errors alert
        error_alerts = [a for a in alerts if a.issue_type == "consecutive_errors"]
        assert len(error_alerts) == 0

    async def test_negative_sentiment_triggers_alert(
        self, detector, mock_store, mock_notifier
    ):
        """Alert when N consecutive negative/frustrated sentiments detected."""
        aggregates = make_aggregates(
            recent_sentiments=["negative", "frustrated", "negative", "positive", "neutral"]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(sentiment="negative")

        alerts = await detector.evaluate("bot_test", output)

        sentiment_alerts = [a for a in alerts if a.issue_type == "negative_sentiment"]
        assert len(sentiment_alerts) == 1
        mock_notifier.send_alert.assert_called()

    async def test_negative_sentiment_not_triggered_when_mixed(
        self, detector, mock_store
    ):
        """No alert when sentiment is mixed (not all negative/frustrated)."""
        aggregates = make_aggregates(
            recent_sentiments=["negative", "neutral", "negative", "positive", "neutral"]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(sentiment="negative")

        alerts = await detector.evaluate("bot_test", output)

        sentiment_alerts = [a for a in alerts if a.issue_type == "negative_sentiment"]
        assert len(sentiment_alerts) == 0

    async def test_low_volume_triggers_alert(
        self, detector, mock_store, mock_notifier
    ):
        """Alert when current day volume is below average by threshold %."""
        # Average is 3, with 50% threshold, alert if below 1.5
        aggregates = make_aggregates(daily_volume_7d=[3, 3, 3, 3, 3, 3, 1])
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output()

        alerts = await detector.evaluate("bot_test", output)

        volume_alerts = [a for a in alerts if a.issue_type == "low_volume"]
        assert len(volume_alerts) == 1

    async def test_low_volume_not_triggered_above_threshold(
        self, detector, mock_store
    ):
        """No alert when volume is above threshold."""
        # Average is ~2.86, with 50% threshold alert if below ~1.43
        aggregates = make_aggregates(daily_volume_7d=[3, 3, 3, 3, 3, 3, 2])
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output()

        alerts = await detector.evaluate("bot_test", output)

        volume_alerts = [a for a in alerts if a.issue_type == "low_volume"]
        assert len(volume_alerts) == 0

    async def test_cooldown_suppresses_alert(
        self, detector, mock_store, mock_notifier
    ):
        """Alert suppressed when in cooldown period."""
        aggregates = make_aggregates(
            recent_errors=[True, True, True, False, False]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        # Simulate being in cooldown
        mock_store.is_in_cooldown = AsyncMock(return_value=True)
        output = make_output(bot_error_detected=True)

        alerts = await detector.evaluate("bot_test", output)

        # All alerts should be suppressed due to cooldown
        assert len(alerts) == 0

    async def test_dropoff_triggers_alert(
        self, detector, mock_store, mock_notifier
    ):
        """Alert when drop-off rate exceeds baseline by threshold %."""
        # Baseline: 2/20 = 10%. Threshold at 50% means alert if rate > 15%.
        # Adding another drop-off: (2+1)/(20+1) = 14.3% — not enough.
        # We need baseline low and current rate high.
        # baseline: 1/20 = 5%. threshold: 5% * 1.5 = 7.5%
        # current: (1+1)/(20+1) = 9.5% > 7.5% — triggers!
        aggregates = make_aggregates(
            total_7d=20,
            dropoff_by_stage_7d={"greeting": 1},
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(
            outcome="dropped_off", drop_off_stage="greeting"
        )

        alerts = await detector.evaluate("bot_test", output)

        dropoff_alerts = [a for a in alerts if a.issue_type == "high_dropoff"]
        assert len(dropoff_alerts) == 1

    async def test_unknown_client_uses_defaults(
        self, detector, mock_store, mock_notifier
    ):
        """Unknown client falls back to default thresholds."""
        aggregates = make_aggregates(
            recent_errors=[True, True, True, False, False]
        )
        mock_store.get_rolling_aggregates = AsyncMock(return_value=aggregates)
        output = make_output(bot_error_detected=True)

        alerts = await detector.evaluate("unknown_client", output)

        error_alerts = [a for a in alerts if a.issue_type == "consecutive_errors"]
        assert len(error_alerts) == 1


class TestIsWithinActiveHours:
    """Tests for the is_within_active_hours() method."""

    def test_within_hours_on_valid_day(self, detector):
        """Returns True for time within range on applicable day."""
        active_hours = ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],  # Mon-Fri
        )
        # Wednesday at 12:00
        dt = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is True

    def test_outside_hours_on_valid_day(self, detector):
        """Returns False for time outside range on applicable day."""
        active_hours = ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],
        )
        # Wednesday at 23:00
        dt = datetime(2024, 1, 3, 23, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is False

    def test_within_hours_on_non_applicable_day(self, detector):
        """Returns False for valid time on non-applicable day."""
        active_hours = ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],  # Mon-Fri only
        )
        # Saturday at 12:00
        dt = datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is False

    def test_at_start_time_boundary(self, detector):
        """Returns True at exactly the start time (inclusive)."""
        active_hours = ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],
        )
        # Wednesday at 08:00 exactly
        dt = datetime(2024, 1, 3, 8, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is True

    def test_at_end_time_boundary(self, detector):
        """Returns False at exactly the end time (exclusive)."""
        active_hours = ActiveHours(
            start_time="08:00",
            end_time="22:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],
        )
        # Wednesday at 22:00 exactly
        dt = datetime(2024, 1, 3, 22, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is False

    def test_overnight_range(self, detector):
        """Handles overnight ranges correctly (e.g., 22:00 - 06:00)."""
        active_hours = ActiveHours(
            start_time="22:00",
            end_time="06:00",
            timezone="UTC",
            days=[0, 1, 2, 3, 4],
        )
        # Wednesday at 23:00 (within overnight range)
        dt = datetime(2024, 1, 3, 23, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is True

        # Wednesday at 03:00 (within overnight range, before end)
        dt = datetime(2024, 1, 3, 3, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is True

        # Wednesday at 12:00 (outside overnight range)
        dt = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)
        assert detector.is_within_active_hours(active_hours, dt) is False


class TestCheckInactiveClients:
    """Tests for the check_inactive_clients() method."""

    async def test_skips_clients_without_active_hours(
        self, app_config, mock_store, mock_notifier
    ):
        """Clients without active_hours are skipped entirely."""
        # Override client to have no active hours
        app_config.clients["bot_test"].active_hours = None
        detector = AnomalyDetector(app_config, mock_store, mock_notifier)

        await detector.check_inactive_clients()

        mock_notifier.send_alert.assert_not_called()

    async def test_alerts_on_zero_conversations_during_active_hours(
        self, app_config, mock_store, mock_notifier
    ):
        """Alert triggered when no conversations during active hours."""
        mock_store.get_conversations_since = AsyncMock(return_value=[])
        detector = AnomalyDetector(app_config, mock_store, mock_notifier)

        # Patch datetime to be within active hours (Wednesday at 12:00 UTC)
        with patch(
            "chatbot_monitor.anomaly_detector.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 3, 12, 0, tzinfo=timezone.utc
            )
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)

            await detector.check_inactive_clients()

        mock_notifier.send_alert.assert_called_once()
        alert = mock_notifier.send_alert.call_args[0][0]
        assert alert.issue_type == "inactive_hours"
        assert alert.client_id == "bot_test"

    async def test_no_alert_when_conversations_exist(
        self, app_config, mock_store, mock_notifier
    ):
        """No alert when conversations exist during active hours."""
        mock_store.get_conversations_since = AsyncMock(
            return_value=[make_output()]
        )
        detector = AnomalyDetector(app_config, mock_store, mock_notifier)

        with patch(
            "chatbot_monitor.anomaly_detector.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 3, 12, 0, tzinfo=timezone.utc
            )
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)

            await detector.check_inactive_clients()

        mock_notifier.send_alert.assert_not_called()

    async def test_sends_failure_notification_on_error(
        self, app_config, mock_store, mock_notifier
    ):
        """Sends failure notification via Telegram on dependency error."""
        mock_store.get_conversations_since = AsyncMock(
            side_effect=Exception("DB connection failed")
        )
        detector = AnomalyDetector(app_config, mock_store, mock_notifier)

        with patch(
            "chatbot_monitor.anomaly_detector.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 3, 12, 0, tzinfo=timezone.utc
            )
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)

            await detector.check_inactive_clients()

        mock_notifier.send_alert.assert_called_once()
        alert = mock_notifier.send_alert.call_args[0][0]
        assert alert.issue_type == "check_failure"

    async def test_cooldown_suppresses_inactive_alert(
        self, app_config, mock_store, mock_notifier
    ):
        """Inactive hours alert suppressed when in cooldown."""
        mock_store.get_conversations_since = AsyncMock(return_value=[])
        mock_store.is_in_cooldown = AsyncMock(return_value=True)
        detector = AnomalyDetector(app_config, mock_store, mock_notifier)

        with patch(
            "chatbot_monitor.anomaly_detector.datetime"
        ) as mock_datetime:
            mock_datetime.now.return_value = datetime(
                2024, 1, 3, 12, 0, tzinfo=timezone.utc
            )
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)

            await detector.check_inactive_clients()

        mock_notifier.send_alert.assert_not_called()
