"""APScheduler-based periodic digest generation and delivery.

Implements:
- Digest generation on a configurable cron schedule (default daily at 08:00 UTC)
- NIM synthesis call per client with conversations since last digest
- Clients with zero conversations are omitted from the digest
- Synthesis text cached in digest_log on Telegram failure for retry without re-calling NIM
- Inactive-hours check registration (delegates to AnomalyDetector)
- Data purge job (daily at 03:00 UTC)
- Graceful shutdown
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from chatbot_monitor.config import AppConfig
from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import DigestMessage, DigestSection
from chatbot_monitor.nim_analyzer import NIMAnalyzer
from chatbot_monitor.telegram_notifier import TelegramNotifier

logger = get_logger("digest_scheduler")

# Directory containing prompt templates
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Formatting limits (from design: max 10 bullets per client, max 280 chars per bullet)
MAX_BULLETS_PER_CLIENT = 10
MAX_BULLET_LENGTH = 280


def _parse_cron_expression(cron_expr: str) -> dict[str, str]:
    """Parse a standard cron expression into APScheduler CronTrigger fields.

    Expected format: "minute hour day month day_of_week"
    Example: "0 8 * * *" -> daily at 08:00

    Args:
        cron_expr: A 5-field cron expression string.

    Returns:
        Dict with keys: minute, hour, day, month, day_of_week.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"Invalid cron expression '{cron_expr}': expected 5 fields "
            f"(minute hour day month day_of_week), got {len(fields)}"
        )

    return {
        "minute": fields[0],
        "hour": fields[1],
        "day": fields[2],
        "month": fields[3],
        "day_of_week": fields[4],
    }


def _format_outcome_distribution(conversations: list) -> str:
    """Format outcome distribution from a list of StructuredOutput objects."""
    dist: dict[str, int] = {}
    for conv in conversations:
        outcome = conv.outcome.value if hasattr(conv.outcome, "value") else str(conv.outcome)
        dist[outcome] = dist.get(outcome, 0) + 1
    total = len(conversations)
    lines = []
    for outcome, count in sorted(dist.items(), key=lambda x: -x[1]):
        pct = (count / total * 100) if total > 0 else 0
        lines.append(f"  {outcome}: {count} ({pct:.0f}%)")
    return "\n".join(lines) if lines else "  No data"


def _format_sentiment_distribution(conversations: list) -> str:
    """Format sentiment distribution from a list of StructuredOutput objects."""
    dist: dict[str, int] = {}
    for conv in conversations:
        sentiment = conv.sentiment.value if hasattr(conv.sentiment, "value") else str(conv.sentiment)
        dist[sentiment] = dist.get(sentiment, 0) + 1
    total = len(conversations)
    lines = []
    for sentiment, count in sorted(dist.items(), key=lambda x: -x[1]):
        pct = (count / total * 100) if total > 0 else 0
        lines.append(f"  {sentiment}: {count} ({pct:.0f}%)")
    return "\n".join(lines) if lines else "  No data"


def _format_dropoff_stages(conversations: list) -> str:
    """Format drop-off stage distribution from conversations."""
    dist: dict[str, int] = {}
    for conv in conversations:
        outcome = conv.outcome.value if hasattr(conv.outcome, "value") else str(conv.outcome)
        if outcome == "dropped_off" and conv.drop_off_stage:
            stage = (
                conv.drop_off_stage.value
                if hasattr(conv.drop_off_stage, "value")
                else str(conv.drop_off_stage)
            )
            dist[stage] = dist.get(stage, 0) + 1
    lines = []
    for stage, count in sorted(dist.items(), key=lambda x: -x[1]):
        lines.append(f"  {stage}: {count}")
    return "\n".join(lines) if lines else "  None"


def _parse_bullets_from_response(response_text: str) -> list[str]:
    """Parse bullet points from NIM synthesis response.

    Expects lines prefixed with '•' or '-' or '*'.
    Enforces max 10 bullets and 280 chars per bullet.
    """
    bullets: list[str] = []
    for line in response_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # Strip common bullet prefixes
        for prefix in ("• ", "- ", "* ", "•", "-", "*"):
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        if not line:
            continue
        # Enforce max length
        if len(line) > MAX_BULLET_LENGTH:
            line = line[: MAX_BULLET_LENGTH - 3] + "..."
        bullets.append(line)
        if len(bullets) >= MAX_BULLETS_PER_CLIENT:
            break
    return bullets


class DigestScheduler:
    """APScheduler-based scheduler for digest generation, inactive checks, and purge.

    Registers three cron jobs:
    1. Digest generation at the configured schedule (e.g., daily at 08:00 UTC)
    2. Inactive-hours client check (delegated to anomaly_detector if available)
    3. Data purge (daily at 03:00 UTC)
    """

    def __init__(
        self,
        config: AppConfig,
        store: MemoryStore,
        analyzer: NIMAnalyzer,
        notifier: TelegramNotifier,
        anomaly_detector=None,
    ):
        """Initialize the DigestScheduler.

        Args:
            config: Application configuration.
            store: Memory store for DB access.
            analyzer: NIM analyzer for synthesis calls.
            notifier: Telegram notifier for digest delivery.
            anomaly_detector: Optional AnomalyDetector for inactive-hours checks.
        """
        self._config = config
        self._store = store
        self._analyzer = analyzer
        self._notifier = notifier
        self._anomaly_detector = anomaly_detector
        self._scheduler = AsyncIOScheduler()
        self._digest_prompt_template = _PROMPTS_DIR.joinpath("digest.txt").read_text(
            encoding="utf-8"
        )

    def start(self) -> None:
        """Register APScheduler cron jobs and start the scheduler.

        Jobs registered:
        1. Digest generation - schedule from config (cron expression)
        2. Inactive-hours check - if anomaly_detector is provided, runs at configured interval
        3. Data purge - daily at 03:00 UTC
        """
        # 1. Register digest generation job
        cron_fields = _parse_cron_expression(self._config.digest_schedule)
        self._scheduler.add_job(
            self.generate_digest,
            trigger=CronTrigger(**cron_fields),
            id="digest_generation",
            name="Periodic Digest Generation",
            replace_existing=True,
        )
        logger.info(
            "Digest job registered",
            extra={"schedule": self._config.digest_schedule},
        )

        # 2. Register inactive-hours check (if anomaly_detector available)
        if self._anomaly_detector is not None:
            self._scheduler.add_job(
                self._anomaly_detector.check_inactive_clients,
                trigger=CronTrigger(minute="0"),  # Every hour at minute 0
                id="inactive_hours_check",
                name="Inactive Hours Client Check",
                replace_existing=True,
            )
            logger.info("Inactive-hours check job registered (hourly)")

        # 3. Register data purge job (daily at 03:00 UTC)
        self._scheduler.add_job(
            self._run_purge,
            trigger=CronTrigger(hour="3", minute="0"),
            id="data_purge",
            name="Data Purge (daily at 03:00 UTC)",
            replace_existing=True,
        )
        logger.info("Data purge job registered (daily at 03:00 UTC)")

        # Start the scheduler
        self._scheduler.start()
        logger.info("Digest scheduler started")

    def shutdown(self) -> None:
        """Stop the scheduler gracefully."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Digest scheduler shut down")

    async def generate_digest(self) -> None:
        """Retrieve data per client, call NIM synthesis, format, and deliver via Telegram.

        Workflow:
        1. Check digest_log for undelivered cached synthesis (retry delivery without re-calling NIM)
        2. For each client with conversations since last digest:
           a. Aggregate conversation data
           b. Call NIM with synthesis prompt
           c. Parse bullet points from response
        3. Omit clients with zero conversations
        4. Format DigestMessage and deliver via TelegramNotifier
        5. On Telegram failure: cache synthesis in digest_log for retry on next interval
        6. On NIM failure: log and skip (retry at next interval)
        """
        logger.info("Digest generation started")

        # Step 1: Check for undelivered cached digests
        retry_success = await self._retry_cached_digests()
        if retry_success:
            logger.info("Cached digest retried and delivered successfully")

        # Step 2: Determine period since last successful digest
        last_digest_time = await self._get_last_successful_digest_time()
        if last_digest_time is None:
            # Default to 24 hours ago if no prior digest
            last_digest_time = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        now = datetime.now(timezone.utc)

        # Step 3: For each client, gather and synthesize
        sections: list[DigestSection] = []
        synthesis_texts: dict[str, str] = {}  # client_id -> raw synthesis text

        for client_id, client_config in self._config.clients.items():
            conversations = await self._store.get_conversations_since(
                client_id, last_digest_time
            )

            # Omit clients with zero conversations (Requirement 8.6)
            if not conversations:
                continue

            # Build the synthesis prompt
            prompt = self._build_synthesis_prompt(
                client_config.display_name,
                last_digest_time,
                now,
                conversations,
            )

            # Call NIM for synthesis
            synthesis_text = await self._call_nim_synthesis(prompt, client_id)
            if synthesis_text is None:
                # NIM failure - log and skip this client (retry next interval)
                logger.error(
                    "NIM synthesis failed for client, skipping",
                    extra={"client_id": client_id},
                )
                continue

            # Parse bullets from synthesis response
            bullets = _parse_bullets_from_response(synthesis_text)
            if not bullets:
                logger.warning(
                    "No bullets parsed from NIM synthesis",
                    extra={"client_id": client_id},
                )
                continue

            synthesis_texts[client_id] = synthesis_text
            sections.append(
                DigestSection(
                    client_id=client_id,
                    client_display_name=client_config.display_name,
                    bullets=bullets,
                )
            )

        if not sections:
            logger.info("No clients with conversations in digest period, skipping delivery")
            return

        # Step 4: Build DigestMessage and deliver
        digest = DigestMessage(sections=sections, generated_at=now)
        delivered = await self._notifier.send_digest(digest)

        if delivered:
            # Record successful delivery in digest_log
            await self._record_digest_log(
                client_ids=[s.client_id for s in sections],
                synthesis_text=json.dumps(synthesis_texts, ensure_ascii=False),
                delivered=True,
            )
            logger.info(
                "Digest delivered successfully",
                extra={"client_count": len(sections)},
            )
        else:
            # Telegram failure - cache synthesis for retry (Requirement 8.7)
            await self._record_digest_log(
                client_ids=[s.client_id for s in sections],
                synthesis_text=json.dumps(synthesis_texts, ensure_ascii=False),
                delivered=False,
            )
            logger.error(
                "Digest delivery failed, synthesis cached for retry",
                extra={"client_count": len(sections)},
            )

    async def _retry_cached_digests(self) -> bool:
        """Retry delivery of cached undelivered digests.

        Looks in digest_log for entries with delivered=0, reconstructs the
        digest from cached synthesis text, and attempts delivery.

        Returns:
            True if a cached digest was successfully delivered, False otherwise.
        """
        assert self._store._db is not None, "Database not initialized"

        cursor = await self._store._db.execute(
            """SELECT id, client_ids, synthesis_text
               FROM digest_log
               WHERE delivered = 0
               ORDER BY generated_at DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        if row is None:
            return False

        log_id = row[0]
        client_ids_str = row[1]
        synthesis_text_json = row[2]

        if not synthesis_text_json:
            return False

        try:
            synthesis_texts = json.loads(synthesis_text_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning(
                "Failed to parse cached synthesis text",
                extra={"digest_log_id": log_id},
            )
            return False

        # Reconstruct sections from cached synthesis
        client_ids = client_ids_str.split(",") if client_ids_str else []
        sections: list[DigestSection] = []

        for client_id in client_ids:
            client_id = client_id.strip()
            client_config = self._config.clients.get(client_id)
            if client_config is None:
                continue

            raw_text = synthesis_texts.get(client_id, "")
            if not raw_text:
                continue

            bullets = _parse_bullets_from_response(raw_text)
            if bullets:
                sections.append(
                    DigestSection(
                        client_id=client_id,
                        client_display_name=client_config.display_name,
                        bullets=bullets,
                    )
                )

        if not sections:
            return False

        digest = DigestMessage(
            sections=sections,
            generated_at=datetime.now(timezone.utc),
        )

        delivered = await self._notifier.send_digest(digest)
        if delivered:
            # Mark as delivered
            await self._store._db.execute(
                "UPDATE digest_log SET delivered = 1 WHERE id = ?",
                (log_id,),
            )
            await self._store._db.commit()
            logger.info(
                "Cached digest delivered on retry",
                extra={"digest_log_id": log_id},
            )
            return True

        return False

    async def _get_last_successful_digest_time(self) -> datetime | None:
        """Get the timestamp of the last successfully delivered digest.

        Returns:
            The datetime of the last successful digest, or None if none found.
        """
        assert self._store._db is not None, "Database not initialized"

        cursor = await self._store._db.execute(
            """SELECT generated_at FROM digest_log
               WHERE delivered = 1
               ORDER BY generated_at DESC
               LIMIT 1"""
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None
        return None

    def _build_synthesis_prompt(
        self,
        client_display_name: str,
        period_start: datetime,
        period_end: datetime,
        conversations: list,
    ) -> str:
        """Build the NIM synthesis prompt for a client's digest.

        Args:
            client_display_name: The client's display name.
            period_start: Start of the digest period.
            period_end: End of the digest period.
            conversations: List of StructuredOutput objects for the period.

        Returns:
            The formatted prompt string.
        """
        total = len(conversations)
        bot_error_count = sum(1 for c in conversations if c.bot_error_detected)

        prompt = self._digest_prompt_template.format(
            client_display_name=client_display_name,
            period_start=period_start.strftime("%Y-%m-%d %H:%M UTC"),
            period_end=period_end.strftime("%Y-%m-%d %H:%M UTC"),
            total_conversations=total,
            outcome_distribution=_format_outcome_distribution(conversations),
            sentiment_distribution=_format_sentiment_distribution(conversations),
            dropoff_stages=_format_dropoff_stages(conversations),
            bot_error_count=bot_error_count,
        )
        return prompt

    async def _call_nim_synthesis(self, prompt: str, client_id: str) -> str | None:
        """Call the NIM API with a synthesis prompt.

        Uses the same NIM client as analysis but with the digest synthesis prompt.
        On failure, returns None (caller should log and skip).

        Args:
            prompt: The formatted synthesis prompt.
            client_id: Client identifier for logging.

        Returns:
            The raw synthesis text from NIM, or None on failure.
        """
        try:
            url = f"{self._analyzer._base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._analyzer._api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self._analyzer._model,
                "messages": [
                    {"role": "system", "content": "You are a business intelligence analyst. Respond with bullet points only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 1024,
            }

            response = await self._analyzer._http_client.post(
                url, json=payload, headers=headers, timeout=30
            )
            response.raise_for_status()

            data = response.json()
            # Extract content from OpenAI-compatible response
            if "choices" in data and data["choices"]:
                content = data["choices"][0].get("message", {}).get("content", "")
                return content if content else None
            return None

        except Exception as e:
            logger.error(
                "NIM synthesis call failed",
                extra={"client_id": client_id, "error": str(e)},
            )
            return None

    async def _record_digest_log(
        self,
        client_ids: list[str],
        synthesis_text: str,
        delivered: bool,
    ) -> None:
        """Record a digest generation event in the digest_log table.

        Args:
            client_ids: List of client IDs included in the digest.
            synthesis_text: The cached synthesis text (JSON of all client syntheses).
            delivered: Whether Telegram delivery was successful.
        """
        assert self._store._db is not None, "Database not initialized"

        try:
            await self._store._db.execute(
                """INSERT INTO digest_log (client_ids, synthesis_text, delivered)
                   VALUES (?, ?, ?)""",
                (
                    ",".join(client_ids),
                    synthesis_text,
                    1 if delivered else 0,
                ),
            )
            await self._store._db.commit()
        except Exception as e:
            logger.error(
                "Failed to record digest log",
                extra={"error": str(e)},
            )

    async def _run_purge(self) -> None:
        """Execute the data purge job.

        Calls store.purge_old_records() with default retention:
        - 30 days for dedupe keys
        - 90 days for raw payloads, structured outputs, and flags
        """
        try:
            total_purged = await self._store.purge_old_records()
            logger.info(
                "Data purge completed",
                extra={"total_purged": total_purged},
            )
        except Exception as e:
            logger.error(
                "Data purge failed",
                extra={"error": str(e)},
            )
