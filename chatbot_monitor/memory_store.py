"""SQLite-backed persistence layer for payloads, analyses, and aggregates.

Uses aiosqlite for async access compatible with FastAPI's async runtime.
Implements WAL mode for better concurrent read performance and retry-once
semantics on write failures.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite

from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.models import RollingAggregates, StructuredOutput

logger = get_logger("memory_store")

# SQL schema statements
_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS dedupe_keys (
    key TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS raw_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE NOT NULL,
    client_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (dedupe_key) REFERENCES dedupe_keys(key)
);

CREATE TABLE IF NOT EXISTS structured_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE NOT NULL,
    client_id TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    outcome TEXT NOT NULL,
    drop_off_stage TEXT,
    sentiment TEXT NOT NULL,
    bot_error_detected INTEGER NOT NULL DEFAULT 0,
    bot_error_notes TEXT,
    notable_quote TEXT,
    summary TEXT NOT NULL,
    analyzed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    FOREIGN KEY (dedupe_key) REFERENCES dedupe_keys(key)
);

CREATE TABLE IF NOT EXISTS flag_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    stage TEXT,
    metric_value REAL,
    baseline_value REAL,
    triggered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    cooldown_until TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    client_ids TEXT NOT NULL,
    synthesis_text TEXT,
    delivered INTEGER NOT NULL DEFAULT 0
);
"""

_CREATE_INDICES = """
CREATE INDEX IF NOT EXISTS idx_structured_client_ts ON structured_outputs(client_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_structured_client_outcome ON structured_outputs(client_id, outcome);
CREATE INDEX IF NOT EXISTS idx_raw_client_ts ON raw_payloads(client_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_flag_cooldown ON flag_history(client_id, issue_type, stage, cooldown_until);
CREATE INDEX IF NOT EXISTS idx_dedupe_created ON dedupe_keys(created_at);
"""


class MemoryStore:
    """Async SQLite data access layer for the conversation intelligence monitor.

    Provides deduplication, raw payload storage, structured output persistence,
    rolling aggregate computation, flag/cooldown tracking, and data purging.
    """

    def __init__(self, db_path: str):
        """Initialize the MemoryStore.

        Args:
            db_path: Path to the SQLite database file (or ":memory:" for testing).
        """
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Create tables and indices if not present. Enable WAL mode and foreign keys."""
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row

        # Enable WAL mode for better concurrent read performance
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

        # Create all tables
        await self._db.executescript(_CREATE_TABLES)
        # Create all indices
        await self._db.executescript(_CREATE_INDICES)
        await self._db.commit()

        logger.info("Database initialized", extra={"db_path": self.db_path})

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # ─── Deduplication ────────────────────────────────────────────────────

    async def has_dedupe_key(self, key: str) -> bool:
        """Check if a dedupe key already exists in the store.

        Args:
            key: The SHA-256 dedupe key to check.

        Returns:
            True if the key exists, False otherwise.
        """
        assert self._db is not None, "Database not initialized"
        cursor = await self._db.execute(
            "SELECT 1 FROM dedupe_keys WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row is not None

    async def store_dedupe_key(self, key: str, client_id: str) -> None:
        """Store a new dedupe key. Retry once on write failure.

        Args:
            key: The SHA-256 dedupe key.
            client_id: The client this key belongs to.
        """
        await self._retry_write(
            "INSERT OR IGNORE INTO dedupe_keys (key, client_id) VALUES (?, ?)",
            (key, client_id),
        )

    # ─── Raw Payload Storage ──────────────────────────────────────────────

    async def store_raw_payload(
        self,
        client_id: str,
        dedupe_key: str,
        contact_id: str,
        timestamp: str,
        payload: dict,
    ) -> None:
        """Store the raw webhook payload. Retry once on write failure.

        Args:
            client_id: The client identifier.
            dedupe_key: The associated dedupe key (must exist in dedupe_keys).
            contact_id: The contact identifier from the payload.
            timestamp: The conversation timestamp (ISO 8601).
            payload: The full raw payload dictionary.
        """
        payload_json = json.dumps(payload, default=str, ensure_ascii=False)
        await self._retry_write(
            """INSERT OR IGNORE INTO raw_payloads
               (dedupe_key, client_id, contact_id, timestamp, payload_json)
               VALUES (?, ?, ?, ?, ?)""",
            (dedupe_key, client_id, contact_id, timestamp, payload_json),
        )

    # ─── Structured Output Persistence ────────────────────────────────────

    async def store_structured_output(
        self,
        client_id: str,
        contact_id: str,
        dedupe_key: str,
        timestamp: str,
        output: StructuredOutput,
    ) -> None:
        """Persist a structured output. Updates rolling aggregates in same transaction.

        Implements retry-once on write failure per Requirement 5.2.

        Args:
            client_id: The client identifier.
            contact_id: The contact identifier.
            dedupe_key: The associated dedupe key.
            timestamp: The conversation timestamp.
            output: The StructuredOutput from NIM analysis.
        """
        assert self._db is not None, "Database not initialized"

        try:
            await self._store_structured_output_inner(
                client_id, contact_id, dedupe_key, timestamp, output
            )
        except Exception as e:
            logger.warning(
                "Structured output write failed, retrying once",
                extra={"dedupe_key": dedupe_key, "error": str(e)},
            )
            try:
                await self._store_structured_output_inner(
                    client_id, contact_id, dedupe_key, timestamp, output
                )
            except Exception as retry_err:
                logger.error(
                    "Structured output write failed after retry, skipping aggregate updates",
                    extra={"dedupe_key": dedupe_key, "error": str(retry_err)},
                )
                raise

    async def _store_structured_output_inner(
        self,
        client_id: str,
        contact_id: str,
        dedupe_key: str,
        timestamp: str,
        output: StructuredOutput,
    ) -> None:
        """Internal method to persist structured output within a transaction."""
        assert self._db is not None
        async with self._db.cursor() as cursor:
            await cursor.execute(
                """INSERT INTO structured_outputs
                   (dedupe_key, client_id, contact_id, timestamp, outcome,
                    drop_off_stage, sentiment, bot_error_detected,
                    bot_error_notes, notable_quote, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dedupe_key,
                    client_id,
                    contact_id,
                    timestamp,
                    output.outcome.value,
                    output.drop_off_stage.value if output.drop_off_stage else None,
                    output.sentiment.value,
                    1 if output.bot_error_detected else 0,
                    output.bot_error_notes,
                    output.notable_quote,
                    output.summary,
                ),
            )
        await self._db.commit()

    # ─── Rolling Aggregates (On-Demand SQL) ───────────────────────────────

    async def get_rolling_aggregates(self, client_id: str) -> RollingAggregates:
        """Compute rolling aggregates for a client using on-demand SQL queries.

        Computes 7-day and 30-day windows for daily volume, outcome distribution,
        drop-off by stage, and sentiment distribution.

        Args:
            client_id: The client identifier.

        Returns:
            A RollingAggregates object with computed statistics.
        """
        assert self._db is not None, "Database not initialized"

        # 7-day daily volume
        cursor = await self._db.execute(
            """SELECT DATE(timestamp) as day, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-7 days')
               GROUP BY DATE(timestamp)
               ORDER BY day""",
            (client_id,),
        )
        rows_7d = await cursor.fetchall()
        daily_volume_7d = [row[1] for row in rows_7d]

        # 30-day daily volume
        cursor = await self._db.execute(
            """SELECT DATE(timestamp) as day, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-30 days')
               GROUP BY DATE(timestamp)
               ORDER BY day""",
            (client_id,),
        )
        rows_30d = await cursor.fetchall()
        daily_volume_30d = [row[1] for row in rows_30d]

        # 7-day outcome distribution
        cursor = await self._db.execute(
            """SELECT outcome, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-7 days')
               GROUP BY outcome""",
            (client_id,),
        )
        outcome_7d_rows = await cursor.fetchall()
        outcome_dist_7d = {row[0]: row[1] for row in outcome_7d_rows}

        # 30-day outcome distribution
        cursor = await self._db.execute(
            """SELECT outcome, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-30 days')
               GROUP BY outcome""",
            (client_id,),
        )
        outcome_30d_rows = await cursor.fetchall()
        outcome_dist_30d = {row[0]: row[1] for row in outcome_30d_rows}

        # 7-day drop-off by stage
        cursor = await self._db.execute(
            """SELECT drop_off_stage, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-7 days')
                     AND outcome = 'dropped_off' AND drop_off_stage IS NOT NULL
               GROUP BY drop_off_stage""",
            (client_id,),
        )
        dropoff_7d_rows = await cursor.fetchall()
        dropoff_by_stage_7d = {row[0]: row[1] for row in dropoff_7d_rows}

        # 30-day drop-off by stage
        cursor = await self._db.execute(
            """SELECT drop_off_stage, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-30 days')
                     AND outcome = 'dropped_off' AND drop_off_stage IS NOT NULL
               GROUP BY drop_off_stage""",
            (client_id,),
        )
        dropoff_30d_rows = await cursor.fetchall()
        dropoff_by_stage_30d = {row[0]: row[1] for row in dropoff_30d_rows}

        # 7-day sentiment distribution
        cursor = await self._db.execute(
            """SELECT sentiment, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-7 days')
               GROUP BY sentiment""",
            (client_id,),
        )
        sentiment_7d_rows = await cursor.fetchall()
        sentiment_dist_7d = {row[0]: row[1] for row in sentiment_7d_rows}

        # 30-day sentiment distribution
        cursor = await self._db.execute(
            """SELECT sentiment, COUNT(*) as count
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= datetime('now', '-30 days')
               GROUP BY sentiment""",
            (client_id,),
        )
        sentiment_30d_rows = await cursor.fetchall()
        sentiment_dist_30d = {row[0]: row[1] for row in sentiment_30d_rows}

        # Recent N conversations for consecutive checks (default 10)
        cursor = await self._db.execute(
            """SELECT bot_error_detected, sentiment
               FROM structured_outputs
               WHERE client_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (client_id, 10),
        )
        recent_rows = await cursor.fetchall()
        recent_errors = [bool(row[0]) for row in recent_rows]
        recent_sentiments = [row[1] for row in recent_rows]

        # Totals
        total_7d = sum(daily_volume_7d)
        total_30d = sum(daily_volume_30d)

        return RollingAggregates(
            daily_volume_7d=daily_volume_7d,
            daily_volume_30d=daily_volume_30d,
            outcome_dist_7d=outcome_dist_7d,
            outcome_dist_30d=outcome_dist_30d,
            dropoff_by_stage_7d=dropoff_by_stage_7d,
            dropoff_by_stage_30d=dropoff_by_stage_30d,
            sentiment_dist_7d=sentiment_dist_7d,
            sentiment_dist_30d=sentiment_dist_30d,
            recent_errors=recent_errors,
            recent_sentiments=recent_sentiments,
            total_conversations_7d=total_7d,
            total_conversations_30d=total_30d,
        )

    # ─── Conversation Queries ─────────────────────────────────────────────

    async def get_conversations_since(
        self, client_id: str, since: datetime
    ) -> list[StructuredOutput]:
        """Retrieve all structured outputs for a client since a given time.

        Args:
            client_id: The client identifier.
            since: The datetime threshold (inclusive).

        Returns:
            List of StructuredOutput objects ordered by timestamp ascending.
        """
        assert self._db is not None, "Database not initialized"
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        cursor = await self._db.execute(
            """SELECT outcome, drop_off_stage, sentiment, bot_error_detected,
                      bot_error_notes, notable_quote, summary
               FROM structured_outputs
               WHERE client_id = ? AND timestamp >= ?
               ORDER BY timestamp ASC""",
            (client_id, since_str),
        )
        rows = await cursor.fetchall()
        results = []
        for row in rows:
            results.append(
                StructuredOutput(
                    outcome=row[0],
                    drop_off_stage=row[1],
                    sentiment=row[2],
                    bot_error_detected=bool(row[3]),
                    bot_error_notes=row[4],
                    notable_quote=row[5],
                    summary=row[6],
                )
            )
        return results

    async def get_last_conversation_time(self, client_id: str) -> Optional[datetime]:
        """Get the timestamp of the most recent conversation for a client.

        Args:
            client_id: The client identifier.

        Returns:
            The datetime of the last conversation, or None if no conversations exist.
        """
        assert self._db is not None, "Database not initialized"
        cursor = await self._db.execute(
            """SELECT MAX(timestamp) FROM structured_outputs WHERE client_id = ?""",
            (client_id,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            try:
                return datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                return None
        return None

    # ─── Flag / Cooldown Management ──────────────────────────────────────

    async def record_flag(
        self,
        client_id: str,
        issue_type: str,
        stage: Optional[str],
        metric: float,
        baseline: float,
        cooldown_minutes: int,
    ) -> None:
        """Record an anomaly flag with cooldown expiration.

        Args:
            client_id: The client identifier.
            issue_type: The type of anomaly detected.
            stage: The optional stage (e.g., drop-off stage).
            metric: The current metric value that triggered the flag.
            baseline: The rolling baseline value being compared against.
            cooldown_minutes: Duration in minutes before this flag type can retrigger.
        """
        cooldown_until = (
            datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        await self._retry_write(
            """INSERT INTO flag_history
               (client_id, issue_type, stage, metric_value, baseline_value, cooldown_until)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (client_id, issue_type, stage, metric, baseline, cooldown_until),
        )

    async def is_in_cooldown(
        self, client_id: str, issue_type: str, stage: Optional[str]
    ) -> bool:
        """Check if a flag type is within its cooldown window.

        Args:
            client_id: The client identifier.
            issue_type: The type of anomaly to check.
            stage: The optional stage to check.

        Returns:
            True if a cooldown is still active, False otherwise.
        """
        assert self._db is not None, "Database not initialized"
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        if stage is not None:
            cursor = await self._db.execute(
                """SELECT 1 FROM flag_history
                   WHERE client_id = ? AND issue_type = ? AND stage = ?
                         AND cooldown_until > ?
                   LIMIT 1""",
                (client_id, issue_type, stage, now_str),
            )
        else:
            cursor = await self._db.execute(
                """SELECT 1 FROM flag_history
                   WHERE client_id = ? AND issue_type = ? AND stage IS NULL
                         AND cooldown_until > ?
                   LIMIT 1""",
                (client_id, issue_type, now_str),
            )
        row = await cursor.fetchone()
        return row is not None

    # ─── Data Purging ─────────────────────────────────────────────────────

    async def purge_old_records(
        self, dedupe_days: int = 30, data_days: int = 90
    ) -> int:
        """Purge records older than configured retention periods.

        - Dedupe keys older than dedupe_days (default 30) are removed.
        - Raw payloads, structured outputs, and flag history older than data_days
          (default 90) are removed.

        Args:
            dedupe_days: Days to retain dedupe keys.
            data_days: Days to retain raw payloads, structured outputs, and flags.

        Returns:
            Total number of records purged across all tables.
        """
        assert self._db is not None, "Database not initialized"

        dedupe_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=dedupe_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        data_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=data_days)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        total_purged = 0

        try:
            # Purge old structured outputs first (foreign key on dedupe_key)
            cursor = await self._db.execute(
                "DELETE FROM structured_outputs WHERE analyzed_at < ?",
                (data_cutoff,),
            )
            total_purged += cursor.rowcount

            # Purge old raw payloads
            cursor = await self._db.execute(
                "DELETE FROM raw_payloads WHERE received_at < ?",
                (data_cutoff,),
            )
            total_purged += cursor.rowcount

            # Purge old flag history
            cursor = await self._db.execute(
                "DELETE FROM flag_history WHERE triggered_at < ?",
                (data_cutoff,),
            )
            total_purged += cursor.rowcount

            # Purge old dedupe keys (after removing dependent records)
            cursor = await self._db.execute(
                "DELETE FROM dedupe_keys WHERE created_at < ?",
                (dedupe_cutoff,),
            )
            total_purged += cursor.rowcount

            await self._db.commit()

            logger.info(
                "Purged old records",
                extra={
                    "total_purged": total_purged,
                    "dedupe_cutoff": dedupe_cutoff,
                    "data_cutoff": data_cutoff,
                },
            )
        except Exception as e:
            logger.error(
                "Error during purge operation",
                extra={"error": str(e)},
            )
            raise

        return total_purged

    # ─── Internal Helpers ─────────────────────────────────────────────────

    async def _retry_write(self, sql: str, params: tuple) -> None:
        """Execute a write operation with retry-once on failure.

        Args:
            sql: The SQL statement to execute.
            params: The parameters for the SQL statement.
        """
        assert self._db is not None, "Database not initialized"
        try:
            await self._db.execute(sql, params)
            await self._db.commit()
        except Exception as e:
            logger.warning(
                "Write failed, retrying once",
                extra={"sql": sql[:100], "error": str(e)},
            )
            try:
                await self._db.execute(sql, params)
                await self._db.commit()
            except Exception as retry_err:
                logger.error(
                    "Write failed after retry",
                    extra={"sql": sql[:100], "error": str(retry_err)},
                )
                raise
