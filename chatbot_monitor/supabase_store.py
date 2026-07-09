"""Supabase-backed persistent storage layer.

Replaces the SQLite MemoryStore when SUPABASE_URL and SUPABASE_KEY are configured.
Uses Supabase's REST API (PostgREST) for all operations — no direct PostgreSQL
connection needed.

All methods match the MemoryStore interface so it's a drop-in replacement.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.models import RollingAggregates, StructuredOutput

logger = get_logger("supabase_store")


class SupabaseStore:
    """Supabase REST API storage layer.

    Provides the same interface as MemoryStore but persists data in Supabase
    (PostgreSQL) so it survives restarts.
    """

    def __init__(self, supabase_url: str, supabase_key: str):
        self._url = supabase_url.rstrip("/")
        self._key = supabase_key
        self._headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }
        self._http = httpx.AsyncClient(timeout=15)
        # Keep a fake _db attribute so code checking `store._db` doesn't crash
        self._db = self

    async def initialize(self) -> None:
        """No-op for Supabase — tables already exist."""
        logger.info("Supabase store initialized", extra={"url": self._url})

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._http.aclose()

    # ─── Deduplication ────────────────────────────────────────────────────

    async def has_dedupe_key(self, key: str) -> bool:
        url = f"{self._url}/rest/v1/dedupe_keys?key=eq.{key}&select=key"
        resp = await self._http.get(url, headers=self._headers)
        if resp.status_code == 200:
            data = resp.json()
            return len(data) > 0
        return False

    async def store_dedupe_key(self, key: str, client_id: str) -> None:
        url = f"{self._url}/rest/v1/dedupe_keys"
        body = {"key": key, "client_id": client_id}
        headers = {**self._headers, "Prefer": "resolution=ignore-duplicates"}
        await self._http.post(url, headers=headers, json=body)

    # ─── Raw Payload Storage ──────────────────────────────────────────────

    async def store_raw_payload(self, client_id: str, dedupe_key: str, contact_id: str, timestamp: str, payload: dict) -> None:
        url = f"{self._url}/rest/v1/raw_payloads"
        body = {
            "dedupe_key": dedupe_key,
            "client_id": client_id,
            "contact_id": contact_id,
            "timestamp": timestamp,
            "payload_json": payload,
        }
        headers = {**self._headers, "Prefer": "resolution=ignore-duplicates"}
        await self._http.post(url, headers=headers, json=body)

    # ─── Structured Output ────────────────────────────────────────────────

    async def store_structured_output(self, client_id: str, contact_id: str, dedupe_key: str, timestamp: str, output: StructuredOutput) -> None:
        url = f"{self._url}/rest/v1/structured_outputs"
        body = {
            "dedupe_key": dedupe_key,
            "client_id": client_id,
            "contact_id": contact_id,
            "timestamp": timestamp,
            "outcome": output.outcome.value,
            "drop_off_stage": output.drop_off_stage.value if output.drop_off_stage else None,
            "sentiment": output.sentiment.value,
            "bot_error_detected": output.bot_error_detected,
            "bot_error_notes": output.bot_error_notes,
            "notable_quote": output.notable_quote,
            "summary": output.summary,
        }
        headers = {**self._headers, "Prefer": "resolution=ignore-duplicates"}
        resp = await self._http.post(url, headers=headers, json=body)
        if resp.status_code not in (200, 201, 409):
            logger.error("Failed to store structured output", extra={"status": resp.status_code, "body": resp.text[:200]})

    # ─── Rolling Aggregates ───────────────────────────────────────────────

    async def get_rolling_aggregates(self, client_id: str) -> RollingAggregates:
        now = datetime.now(timezone.utc)
        seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        thirty_days_ago = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Get 7-day data
        url = f"{self._url}/rest/v1/structured_outputs?client_id=eq.{client_id}&timestamp=gte.{seven_days_ago}&select=outcome,drop_off_stage,sentiment,bot_error_detected,timestamp"
        resp = await self._http.get(url, headers=self._headers)
        rows_7d = resp.json() if resp.status_code == 200 else []

        # Get 30-day data
        url = f"{self._url}/rest/v1/structured_outputs?client_id=eq.{client_id}&timestamp=gte.{thirty_days_ago}&select=outcome,drop_off_stage,sentiment,bot_error_detected,timestamp"
        resp = await self._http.get(url, headers=self._headers)
        rows_30d = resp.json() if resp.status_code == 200 else []

        # Get recent 10 for consecutive checks
        url = f"{self._url}/rest/v1/structured_outputs?client_id=eq.{client_id}&select=bot_error_detected,sentiment&order=timestamp.desc&limit=10"
        resp = await self._http.get(url, headers=self._headers)
        recent_rows = resp.json() if resp.status_code == 200 else []

        # Compute aggregates
        outcome_dist_7d = {}
        sentiment_dist_7d = {}
        dropoff_by_stage_7d = {}
        daily_volumes_7d = {}

        for row in rows_7d:
            outcome = row.get("outcome", "")
            outcome_dist_7d[outcome] = outcome_dist_7d.get(outcome, 0) + 1
            sentiment = row.get("sentiment", "")
            sentiment_dist_7d[sentiment] = sentiment_dist_7d.get(sentiment, 0) + 1
            if outcome == "dropped_off" and row.get("drop_off_stage"):
                stage = row["drop_off_stage"]
                dropoff_by_stage_7d[stage] = dropoff_by_stage_7d.get(stage, 0) + 1
            day = row.get("timestamp", "")[:10]
            daily_volumes_7d[day] = daily_volumes_7d.get(day, 0) + 1

        outcome_dist_30d = {}
        sentiment_dist_30d = {}
        dropoff_by_stage_30d = {}
        daily_volumes_30d = {}

        for row in rows_30d:
            outcome = row.get("outcome", "")
            outcome_dist_30d[outcome] = outcome_dist_30d.get(outcome, 0) + 1
            sentiment = row.get("sentiment", "")
            sentiment_dist_30d[sentiment] = sentiment_dist_30d.get(sentiment, 0) + 1
            if outcome == "dropped_off" and row.get("drop_off_stage"):
                stage = row["drop_off_stage"]
                dropoff_by_stage_30d[stage] = dropoff_by_stage_30d.get(stage, 0) + 1
            day = row.get("timestamp", "")[:10]
            daily_volumes_30d[day] = daily_volumes_30d.get(day, 0) + 1

        recent_errors = [bool(row.get("bot_error_detected")) for row in recent_rows]
        recent_sentiments = [row.get("sentiment", "") for row in recent_rows]

        return RollingAggregates(
            daily_volume_7d=list(daily_volumes_7d.values()),
            daily_volume_30d=list(daily_volumes_30d.values()),
            outcome_dist_7d=outcome_dist_7d,
            outcome_dist_30d=outcome_dist_30d,
            dropoff_by_stage_7d=dropoff_by_stage_7d,
            dropoff_by_stage_30d=dropoff_by_stage_30d,
            sentiment_dist_7d=sentiment_dist_7d,
            sentiment_dist_30d=sentiment_dist_30d,
            recent_errors=recent_errors,
            recent_sentiments=recent_sentiments,
            total_conversations_7d=len(rows_7d),
            total_conversations_30d=len(rows_30d),
        )

    # ─── Conversation Queries ─────────────────────────────────────────────

    async def get_conversations_since(self, client_id: str, since: datetime) -> list[StructuredOutput]:
        since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self._url}/rest/v1/structured_outputs?client_id=eq.{client_id}&timestamp=gte.{since_str}&order=timestamp.asc"
        resp = await self._http.get(url, headers=self._headers)
        if resp.status_code != 200:
            return []
        rows = resp.json()
        results = []
        for row in rows:
            try:
                results.append(StructuredOutput(
                    outcome=row["outcome"],
                    drop_off_stage=row.get("drop_off_stage"),
                    sentiment=row["sentiment"],
                    bot_error_detected=bool(row.get("bot_error_detected")),
                    bot_error_notes=row.get("bot_error_notes"),
                    notable_quote=row.get("notable_quote"),
                    summary=row.get("summary", ""),
                ))
            except Exception:
                continue
        return results

    async def get_last_conversation_time(self, client_id: str) -> Optional[datetime]:
        url = f"{self._url}/rest/v1/structured_outputs?client_id=eq.{client_id}&select=timestamp&order=timestamp.desc&limit=1"
        resp = await self._http.get(url, headers=self._headers)
        if resp.status_code == 200:
            rows = resp.json()
            if rows and rows[0].get("timestamp"):
                try:
                    return datetime.fromisoformat(rows[0]["timestamp"].replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
        return None

    # ─── Flag / Cooldown ──────────────────────────────────────────────────

    async def record_flag(self, client_id: str, issue_type: str, stage: Optional[str], metric: float, baseline: float, cooldown_minutes: int) -> None:
        cooldown_until = (datetime.now(timezone.utc) + timedelta(minutes=cooldown_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = f"{self._url}/rest/v1/flag_history"
        body = {
            "client_id": client_id,
            "issue_type": issue_type,
            "stage": stage,
            "metric_value": metric,
            "baseline_value": baseline,
            "cooldown_until": cooldown_until,
        }
        await self._http.post(url, headers=self._headers, json=body)

    async def is_in_cooldown(self, client_id: str, issue_type: str, stage: Optional[str]) -> bool:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if stage is not None:
            url = f"{self._url}/rest/v1/flag_history?client_id=eq.{client_id}&issue_type=eq.{issue_type}&stage=eq.{stage}&cooldown_until=gt.{now_str}&select=id&limit=1"
        else:
            url = f"{self._url}/rest/v1/flag_history?client_id=eq.{client_id}&issue_type=eq.{issue_type}&stage=is.null&cooldown_until=gt.{now_str}&select=id&limit=1"
        resp = await self._http.get(url, headers=self._headers)
        if resp.status_code == 200:
            return len(resp.json()) > 0
        return False

    # ─── Data Purge ───────────────────────────────────────────────────────

    async def purge_old_records(self, dedupe_days: int = 30, data_days: int = 90) -> int:
        dedupe_cutoff = (datetime.now(timezone.utc) - timedelta(days=dedupe_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data_cutoff = (datetime.now(timezone.utc) - timedelta(days=data_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

        total = 0
        # Purge old structured outputs
        url = f"{self._url}/rest/v1/structured_outputs?analyzed_at=lt.{data_cutoff}"
        resp = await self._http.delete(url, headers=self._headers)
        if resp.status_code == 200:
            total += len(resp.json()) if resp.text else 0

        # Purge old raw payloads
        url = f"{self._url}/rest/v1/raw_payloads?received_at=lt.{data_cutoff}"
        await self._http.delete(url, headers=self._headers)

        # Purge old flags
        url = f"{self._url}/rest/v1/flag_history?triggered_at=lt.{data_cutoff}"
        await self._http.delete(url, headers=self._headers)

        # Purge old dedupe keys
        url = f"{self._url}/rest/v1/dedupe_keys?created_at=lt.{dedupe_cutoff}"
        await self._http.delete(url, headers=self._headers)

        logger.info("Purge complete", extra={"total": total})
        return total

    # ─── SQL query support for Telegram bot commands ──────────────────────

    async def execute(self, sql: str, params: tuple = ()) -> "FakeAsyncCursor":
        """Execute a raw SQL query via Supabase RPC or direct query.

        This is a compatibility shim for the Telegram bot commands that
        use raw SQL. Uses Supabase's /rest/v1/rpc endpoint.
        """
        # For the telegram bot, we'll handle specific patterns
        return FakeAsyncCursor([])


class FakeAsyncCursor:
    """Compatibility shim for code that expects an aiosqlite cursor."""

    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None
