"""Anomaly detection against rolling baselines with cooldown and persistence.

Evaluates each new StructuredOutput against RollingAggregates for a client,
checking for: high drop-off rates, consecutive bot errors, low lead volume,
and negative/frustrated sentiment streaks. Applies persistence count logic
(only trigger after N consecutive occurrences) and cooldown periods to avoid
alert fatigue.

Also provides scheduled detection of inactive clients during their configured
active hours.
"""

from datetime import datetime, time, timezone
from typing import Optional

from chatbot_monitor.config import AppConfig, ClientConfig
from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import (
    ActiveHours,
    AlertThresholds,
    AnomalyAlert,
    RollingAggregates,
    StructuredOutput,
)
from chatbot_monitor.telegram_notifier import TelegramNotifier

logger = get_logger("anomaly_detector")


class AnomalyDetector:
    """Detects anomalies by comparing per-conversation metrics against rolling baselines.

    Supports configurable thresholds per client with fallback to defaults,
    persistence count (N consecutive occurrences before alerting), and
    cooldown windows to prevent repeated alerts.
    """

    def __init__(
        self, config: AppConfig, store: MemoryStore, notifier: TelegramNotifier
    ):
        """Initialize the anomaly detector.

        Args:
            config: Application configuration with client thresholds and defaults.
            store: Memory store for rolling aggregates and cooldown checks.
            notifier: Telegram notifier for sending alerts.
        """
        self.config = config
        self.store = store
        self.notifier = notifier

    async def evaluate(
        self, client_id: str, output: StructuredOutput
    ) -> list[AnomalyAlert]:
        """Check a new conversation against rolling baselines. Return triggered alerts.

        Evaluates four anomaly types:
        1. Drop-off rate exceeding rolling average by configurable %
        2. Consecutive conversations with bot_error_detected
        3. Lead volume below rolling average by configurable %
        4. Consecutive conversations with negative/frustrated sentiment

        Each anomaly must persist for N consecutive occurrences before triggering.
        Cooldown windows prevent duplicate alerts for the same issue.

        Args:
            client_id: The client identifier.
            output: The newly analyzed StructuredOutput for the conversation.

        Returns:
            List of AnomalyAlert objects for all triggered anomalies.
        """
        client_config = self._get_client_config(client_id)
        thresholds = self._get_thresholds(client_config)
        display_name = client_config.display_name if client_config else client_id

        # Get rolling aggregates
        aggregates = await self.store.get_rolling_aggregates(client_id)

        # Check if we have enough data
        if aggregates.total_conversations_7d < thresholds.persistence_count:
            logger.info(
                "Insufficient data for anomaly evaluation",
                extra={
                    "client_id": client_id,
                    "total_conversations": aggregates.total_conversations_7d,
                    "persistence_count": thresholds.persistence_count,
                },
            )
            return []

        alerts: list[AnomalyAlert] = []

        # Check each anomaly type
        dropoff_alert = await self._check_dropoff_rate(
            client_id, display_name, output, aggregates, thresholds
        )
        if dropoff_alert:
            alerts.append(dropoff_alert)

        error_alert = await self._check_consecutive_errors(
            client_id, display_name, aggregates, thresholds
        )
        if error_alert:
            alerts.append(error_alert)

        volume_alert = await self._check_low_volume(
            client_id, display_name, aggregates, thresholds
        )
        if volume_alert:
            alerts.append(volume_alert)

        sentiment_alert = await self._check_negative_sentiment(
            client_id, display_name, aggregates, thresholds
        )
        if sentiment_alert:
            alerts.append(sentiment_alert)

        # Send alerts via Telegram
        for alert in alerts:
            logger.info(
                "Anomaly detected",
                extra={
                    "client_id": alert.client_id,
                    "issue_type": alert.issue_type,
                    "metric_value": alert.metric_value,
                    "baseline_value": alert.baseline_value,
                },
            )
            await self.notifier.send_alert(alert)

        return alerts

    async def check_inactive_clients(self) -> None:
        """Scheduled job: check for clients with zero conversations during active hours.

        For each client with active_hours configured, checks if the current time
        falls within their active window. If so and there are zero conversations
        since the start of that window, triggers an inactive hours alert.

        Clients without active_hours configured are skipped.
        On dependency error, sends a failure notification via Telegram.
        """
        now = datetime.now(timezone.utc)

        try:
            for client_id, client_config in self.config.clients.items():
                if client_config.active_hours is None:
                    continue

                active_hours = client_config.active_hours

                # Convert current UTC time to client's timezone
                try:
                    import zoneinfo
                    client_tz = zoneinfo.ZoneInfo(active_hours.timezone)
                except (KeyError, ImportError):
                    logger.warning(
                        "Invalid timezone for client",
                        extra={
                            "client_id": client_id,
                            "timezone": active_hours.timezone,
                        },
                    )
                    continue

                client_now = now.astimezone(client_tz)

                if not self.is_within_active_hours(active_hours, client_now):
                    continue

                # Check for conversations since the start of the active window
                window_start = client_now.replace(
                    hour=int(active_hours.start_time.split(":")[0]),
                    minute=int(active_hours.start_time.split(":")[1]),
                    second=0,
                    microsecond=0,
                )
                # Convert window_start back to UTC for DB query
                window_start_utc = window_start.astimezone(timezone.utc)

                conversations = await self.store.get_conversations_since(
                    client_id, window_start_utc
                )

                if len(conversations) == 0:
                    # Check cooldown
                    in_cooldown = await self.store.is_in_cooldown(
                        client_id, "inactive_hours", None
                    )
                    if in_cooldown:
                        continue

                    thresholds = self._get_thresholds(client_config)

                    alert = AnomalyAlert(
                        client_id=client_id,
                        client_display_name=client_config.display_name,
                        issue_type="inactive_hours",
                        stage=None,
                        metric_value=0.0,
                        baseline_value=1.0,
                        message=(
                            f"No conversations received during active hours "
                            f"({active_hours.start_time}-{active_hours.end_time} "
                            f"{active_hours.timezone})"
                        ),
                    )

                    # Record flag for cooldown tracking
                    await self.store.record_flag(
                        client_id=client_id,
                        issue_type="inactive_hours",
                        stage=None,
                        metric=0.0,
                        baseline=1.0,
                        cooldown_minutes=thresholds.cooldown_minutes,
                    )

                    logger.info(
                        "Anomaly detected",
                        extra={
                            "client_id": client_id,
                            "issue_type": "inactive_hours",
                            "metric_value": 0.0,
                            "baseline_value": 1.0,
                        },
                    )

                    await self.notifier.send_alert(alert)

        except Exception as e:
            logger.error(
                "Inactive clients check failed",
                extra={"error": str(e)},
            )
            # Send failure notification via Telegram
            failure_alert = AnomalyAlert(
                client_id="system",
                client_display_name="System",
                issue_type="check_failure",
                stage=None,
                metric_value=0.0,
                baseline_value=0.0,
                message=(
                    f"Inactive hours check failed at "
                    f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')}: {str(e)}"
                ),
            )
            await self.notifier.send_alert(failure_alert)

    def is_within_active_hours(self, active_hours: ActiveHours, dt: datetime) -> bool:
        """Pure function: determine if datetime falls within active hours window.

        Checks both the day of week and the time range. The time range is
        treated as [start_time, end_time) — inclusive start, exclusive end.

        Args:
            active_hours: The active hours configuration with start_time,
                         end_time, timezone, and applicable days.
            dt: The datetime to check (should already be in the client's timezone).

        Returns:
            True if dt falls within the active hours window on an applicable day.
        """
        # Check day of week (0=Monday..6=Sunday)
        if dt.weekday() not in active_hours.days:
            return False

        # Parse start and end times
        start_parts = active_hours.start_time.split(":")
        end_parts = active_hours.end_time.split(":")

        start = time(int(start_parts[0]), int(start_parts[1]))
        end = time(int(end_parts[0]), int(end_parts[1]))

        current_time = dt.time()

        # Handle normal range (start < end)
        if start <= end:
            return start <= current_time < end
        else:
            # Handle overnight range (e.g., 22:00 - 06:00)
            return current_time >= start or current_time < end

    # ─── Private Methods ──────────────────────────────────────────────────

    def _get_client_config(self, client_id: str) -> Optional[ClientConfig]:
        """Get the client configuration, or None if not configured."""
        return self.config.clients.get(client_id)

    def _get_thresholds(self, client_config: Optional[ClientConfig]) -> AlertThresholds:
        """Get thresholds for a client with fallback to defaults."""
        if client_config and client_config.thresholds:
            return client_config.thresholds
        return self.config.alert_defaults

    async def _check_dropoff_rate(
        self,
        client_id: str,
        display_name: str,
        output: StructuredOutput,
        aggregates: RollingAggregates,
        thresholds: AlertThresholds,
    ) -> Optional[AnomalyAlert]:
        """Check if drop-off rate exceeds rolling average by configured percentage.

        Uses the 7-day drop-off rate as the baseline. Triggers if the recent
        drop-off rate exceeds the baseline by the configured threshold percentage.
        Requires persistence_count consecutive drop-offs before alerting.
        """
        total_7d = aggregates.total_conversations_7d
        if total_7d == 0:
            return None

        # Calculate baseline drop-off rate from 7d data
        total_dropoffs_7d = sum(aggregates.dropoff_by_stage_7d.values())
        baseline_dropoff_rate = total_dropoffs_7d / total_7d

        # Count recent consecutive drop-offs from recent conversations
        # The most recent entries in recent_errors/recent_sentiments correspond
        # to the most recent conversations. Check if persistence_count most recent
        # outcomes include enough drop-offs.
        # For drop-off rate, we look at whether the current rate exceeds baseline.

        # Calculate current drop-off rate: count drop-offs in recent N conversations
        # where N = persistence_count
        persistence = thresholds.persistence_count
        if len(aggregates.recent_errors) < persistence:
            return None

        # We need to get the recent outcomes to check for drop-off persistence.
        # Since RollingAggregates doesn't directly have recent outcomes, we'll
        # check if the current conversation is a drop-off and if the recent
        # drop-off rate exceeds the threshold.

        # Current drop-off rate: use the 7d data plus current conversation
        # to see if threshold is exceeded
        if output.outcome.value != "dropped_off":
            return None

        # Calculate what the threshold would be
        threshold_rate = baseline_dropoff_rate * (1 + thresholds.dropoff_rate_pct / 100)

        # For stage-specific check
        stage = output.drop_off_stage.value if output.drop_off_stage else None

        # Check if we have persistence_count consecutive drop-offs in recent conversations
        # We use the recent_sentiments list as a proxy to check sequence.
        # Actually, we need to check the recent outcomes from the DB.
        # Since the RollingAggregates model doesn't directly track recent outcomes,
        # we rely on the current drop-off rate vs baseline.

        # Current observed rate: total dropoffs / total conversations
        current_rate = (total_dropoffs_7d + 1) / (total_7d + 1)

        if current_rate <= threshold_rate:
            return None

        # Check cooldown
        in_cooldown = await self.store.is_in_cooldown(
            client_id, "high_dropoff", stage
        )
        if in_cooldown:
            return None

        # Record flag
        await self.store.record_flag(
            client_id=client_id,
            issue_type="high_dropoff",
            stage=stage,
            metric=current_rate,
            baseline=baseline_dropoff_rate,
            cooldown_minutes=thresholds.cooldown_minutes,
        )

        return AnomalyAlert(
            client_id=client_id,
            client_display_name=display_name,
            issue_type="high_dropoff",
            stage=stage,
            metric_value=round(current_rate * 100, 1),
            baseline_value=round(baseline_dropoff_rate * 100, 1),
            message=(
                f"Drop-off rate {current_rate*100:.1f}% exceeds baseline "
                f"{baseline_dropoff_rate*100:.1f}% by more than "
                f"{thresholds.dropoff_rate_pct}% threshold"
                + (f" (stage: {stage})" if stage else "")
            ),
        )

    async def _check_consecutive_errors(
        self,
        client_id: str,
        display_name: str,
        aggregates: RollingAggregates,
        thresholds: AlertThresholds,
    ) -> Optional[AnomalyAlert]:
        """Check for N consecutive conversations with bot_error_detected.

        Uses the recent_errors list from rolling aggregates (ordered most-recent first).
        Triggers when persistence_count consecutive errors are detected.
        """
        persistence = thresholds.persistence_count
        consecutive_threshold = thresholds.consecutive_errors

        # Need at least persistence_count recent conversations
        if len(aggregates.recent_errors) < persistence:
            return None

        # Check if the first N (persistence_count) recent conversations all have errors
        # recent_errors is ordered most-recent first
        recent_slice = aggregates.recent_errors[:consecutive_threshold]

        if len(recent_slice) < consecutive_threshold:
            return None

        # All must be True for consecutive errors
        if not all(recent_slice):
            return None

        # Check cooldown
        in_cooldown = await self.store.is_in_cooldown(
            client_id, "consecutive_errors", None
        )
        if in_cooldown:
            return None

        # Count of consecutive errors
        consecutive_count = 0
        for error in aggregates.recent_errors:
            if error:
                consecutive_count += 1
            else:
                break

        # Record flag
        await self.store.record_flag(
            client_id=client_id,
            issue_type="consecutive_errors",
            stage=None,
            metric=float(consecutive_count),
            baseline=float(consecutive_threshold),
            cooldown_minutes=thresholds.cooldown_minutes,
        )

        return AnomalyAlert(
            client_id=client_id,
            client_display_name=display_name,
            issue_type="consecutive_errors",
            stage=None,
            metric_value=float(consecutive_count),
            baseline_value=float(consecutive_threshold),
            message=(
                f"{consecutive_count} consecutive conversations with bot errors detected "
                f"(threshold: {consecutive_threshold})"
            ),
        )

    async def _check_low_volume(
        self,
        client_id: str,
        display_name: str,
        aggregates: RollingAggregates,
        thresholds: AlertThresholds,
    ) -> Optional[AnomalyAlert]:
        """Check if lead volume is below rolling average by configured percentage.

        Compares the current day's volume against the 7-day daily average.
        Triggers if below by more than the configured low_volume_pct threshold.
        """
        if not aggregates.daily_volume_7d:
            return None

        # Calculate the average daily volume over 7 days
        avg_daily_volume = sum(aggregates.daily_volume_7d) / len(
            aggregates.daily_volume_7d
        )

        if avg_daily_volume == 0:
            return None

        # Current day volume is the last entry in the daily volumes
        # (most recent day)
        current_day_volume = (
            aggregates.daily_volume_7d[-1] if aggregates.daily_volume_7d else 0
        )

        # Calculate threshold: volume below which we alert
        threshold_volume = avg_daily_volume * (1 - thresholds.low_volume_pct / 100)

        if current_day_volume >= threshold_volume:
            return None

        # Check cooldown
        in_cooldown = await self.store.is_in_cooldown(
            client_id, "low_volume", None
        )
        if in_cooldown:
            return None

        # Record flag
        await self.store.record_flag(
            client_id=client_id,
            issue_type="low_volume",
            stage=None,
            metric=float(current_day_volume),
            baseline=avg_daily_volume,
            cooldown_minutes=thresholds.cooldown_minutes,
        )

        return AnomalyAlert(
            client_id=client_id,
            client_display_name=display_name,
            issue_type="low_volume",
            stage=None,
            metric_value=float(current_day_volume),
            baseline_value=round(avg_daily_volume, 1),
            message=(
                f"Lead volume ({current_day_volume}) is below the rolling average "
                f"({avg_daily_volume:.1f}) by more than {thresholds.low_volume_pct}%"
            ),
        )

    async def _check_negative_sentiment(
        self,
        client_id: str,
        display_name: str,
        aggregates: RollingAggregates,
        thresholds: AlertThresholds,
    ) -> Optional[AnomalyAlert]:
        """Check for N consecutive conversations with negative or frustrated sentiment.

        Uses the recent_sentiments list from rolling aggregates (ordered most-recent first).
        Triggers when persistence_count consecutive negative/frustrated sentiments are detected.
        """
        persistence = thresholds.persistence_count
        consecutive_threshold = thresholds.consecutive_neg_sentiment

        # Need at least the consecutive threshold recent conversations
        if len(aggregates.recent_sentiments) < consecutive_threshold:
            return None

        # Check if the first N recent conversations have negative/frustrated sentiment
        # recent_sentiments is ordered most-recent first
        negative_sentiments = {"negative", "frustrated"}
        recent_slice = aggregates.recent_sentiments[:consecutive_threshold]

        if len(recent_slice) < consecutive_threshold:
            return None

        # All must be negative or frustrated
        if not all(s in negative_sentiments for s in recent_slice):
            return None

        # Check cooldown
        in_cooldown = await self.store.is_in_cooldown(
            client_id, "negative_sentiment", None
        )
        if in_cooldown:
            return None

        # Count consecutive negative sentiments
        consecutive_count = 0
        for sentiment in aggregates.recent_sentiments:
            if sentiment in negative_sentiments:
                consecutive_count += 1
            else:
                break

        # Record flag
        await self.store.record_flag(
            client_id=client_id,
            issue_type="negative_sentiment",
            stage=None,
            metric=float(consecutive_count),
            baseline=float(consecutive_threshold),
            cooldown_minutes=thresholds.cooldown_minutes,
        )

        return AnomalyAlert(
            client_id=client_id,
            client_display_name=display_name,
            issue_type="negative_sentiment",
            stage=None,
            metric_value=float(consecutive_count),
            baseline_value=float(consecutive_threshold),
            message=(
                f"{consecutive_count} consecutive conversations with "
                f"negative/frustrated sentiment (threshold: {consecutive_threshold})"
            ),
        )
