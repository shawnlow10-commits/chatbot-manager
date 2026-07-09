"""Chatrace API client for writing analysis results back to contacts.

Runs AFTER NIM analysis completes. Adds tags, writes custom fields,
and creates pipeline opportunities based on analysis results.
Never blocks or affects the existing webhook/analysis/alert flow.

API docs: https://api.chatrace.com/swagger/
"""

import httpx

from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.models import StructuredOutput

logger = get_logger("chatrace_api")

CHATRACE_BASE_URL = "https://api.chatrace.com"


class ChatraceClient:
    """Client for Chatrace API operations.

    Performs best-effort actions — if any call fails, it logs and continues.
    Never raises exceptions to the caller.
    """

    def __init__(self, api_token: str, http_client: httpx.AsyncClient):
        """Initialize the Chatrace API client.

        Args:
            api_token: Chatrace API token for authentication.
            http_client: Shared httpx async client.
        """
        self._token = api_token
        self._http = http_client
        self._headers = {
            "X-ACCESS-TOKEN": api_token,
            "Content-Type": "application/json",
        }
        self._auth_params = {}

    async def bulk_sync_contacts(self, store, analyzer, client_id: str) -> int:
        """Pull contacts from Chatrace and re-analyze their chat histories.

        Called on startup to repopulate the local DB after a restart.
        Uses find_by_custom_field to get contacts with chat history,
        then re-runs NIM analysis and stores results locally.

        Args:
            store: MemoryStore instance to persist results.
            analyzer: NIMAnalyzer instance for analysis.
            client_id: The client identifier.

        Returns:
            Number of contacts successfully synced.
        """
        if not self._token:
            return 0

        logger.info("Starting bulk sync from Chatrace", extra={"client_id": client_id})
        synced = 0

        try:
            # Try to find contacts using the custom field search
            # Chatrace API: GET /contacts/find_by_custom_field?custom_field_id=XXX&value=XXX
            # We'll try fetching contacts that have the "chat history" field
            chat_history_field_id = await self._get_custom_field_id_by_name("chat history")
            if not chat_history_field_id:
                logger.warning("Could not find 'chat history' custom field ID")
                return 0

            # Query contacts with that field populated
            url = f"{CHATRACE_BASE_URL}/contacts/find_by_custom_field"
            params = {"custom_field_id": chat_history_field_id, **self._auth_params}
            response = await self._http.get(url, headers=self._headers, params=params, timeout=30)

            if response.status_code != 200:
                logger.warning(
                    "Chatrace find_by_custom_field returned non-200",
                    extra={"status_code": response.status_code},
                )
                return 0

            data = response.json()
            contacts = data if isinstance(data, list) else data.get("data", data.get("contacts", []))

            if not contacts:
                logger.info("No contacts found with chat history in Chatrace")
                return 0

            logger.info(f"Found {len(contacts)} contacts to sync")

            # Process each contact (limit to most recent 50 to avoid hammering NIM)
            for contact in contacts[:50]:
                try:
                    contact_id = contact.get("phone") or contact.get("id") or ""
                    if not contact_id:
                        continue

                    # Check if we already have this contact analyzed
                    # Use a simple dedupe: check if contact_id exists in structured_outputs
                    assert store._db is not None
                    cursor = await store._db.execute(
                        "SELECT 1 FROM structured_outputs WHERE contact_id = ? LIMIT 1",
                        (str(contact_id),),
                    )
                    if await cursor.fetchone():
                        continue  # Already synced

                    # Get chat history from custom fields
                    chat_history_text = ""
                    custom_fields = contact.get("custom_fields", [])
                    if isinstance(custom_fields, list):
                        for field in custom_fields:
                            if isinstance(field, dict):
                                if field.get("name", "").lower().strip() == "chat history":
                                    chat_history_text = field.get("value", "")
                                    break

                    # If no chat history in the bulk response, try fetching individually
                    if not chat_history_text:
                        individual = await self._get_contact_with_fields(str(contact_id))
                        if individual:
                            custom_fields = individual.get("custom_fields", [])
                            for field in custom_fields:
                                if isinstance(field, dict):
                                    if field.get("name", "").lower().strip() == "chat history":
                                        chat_history_text = field.get("value", "")
                                        break

                    if not chat_history_text:
                        continue

                    # Parse chat history
                    from chatbot_monitor.validator import _parse_chatrace_chat_history
                    messages = _parse_chatrace_chat_history(chat_history_text)
                    if not messages:
                        continue

                    # Get timestamp
                    last_interaction = contact.get("last_interaction", "")
                    if last_interaction:
                        try:
                            from datetime import datetime, timezone
                            ts_seconds = int(last_interaction) / 1000.0
                            dt = datetime.fromtimestamp(ts_seconds, tz=timezone.utc)
                            timestamp = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        except (ValueError, TypeError):
                            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    else:
                        from datetime import datetime, timezone
                        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    # Compute dedupe key
                    from chatbot_monitor.deduplicator import compute_dedupe_key
                    dedupe_key = compute_dedupe_key(client_id, str(contact_id), timestamp)

                    # Check dedupe
                    if await store.has_dedupe_key(dedupe_key):
                        continue

                    # Analyze with NIM
                    result = await analyzer.analyze(
                        chat_history=messages,
                        client_id=client_id,
                        dedupe_key=dedupe_key,
                    )

                    if result is None:
                        continue

                    # Store
                    await store.store_dedupe_key(dedupe_key, client_id)
                    await store.store_structured_output(
                        client_id=client_id,
                        contact_id=str(contact_id),
                        dedupe_key=dedupe_key,
                        timestamp=timestamp,
                        output=result,
                    )
                    synced += 1
                    logger.info(
                        "Synced contact",
                        extra={"contact_id": contact_id, "outcome": result.outcome.value},
                    )

                except Exception as e:
                    logger.warning(
                        "Failed to sync individual contact",
                        extra={"error": str(e)},
                    )
                    continue

        except Exception as e:
            logger.error("Bulk sync failed", extra={"error": str(e)})

        logger.info(f"Bulk sync complete: {synced} contacts synced")
        return synced

    async def _get_contact_with_fields(self, contact_id: str) -> dict | None:
        """Fetch a single contact with all their custom fields."""
        try:
            url = f"{CHATRACE_BASE_URL}/contacts/{contact_id}"
            response = await self._http.get(url, headers=self._headers, params=self._auth_params, timeout=15)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.debug("Failed to fetch contact", extra={"contact_id": contact_id, "error": str(e)})
        return None

    async def sync_analysis_to_contact(
        self,
        contact_id: str,
        analysis: StructuredOutput,
        client_id: str,
    ) -> None:
        """Write NIM analysis results back to the Chatrace contact.

        Actions (all best-effort, failures logged and skipped):
        1. Tag the contact based on outcome
        2. Write analysis summary to a custom field

        Args:
            contact_id: The Chatrace contact ID (phone number or ID).
            analysis: The structured output from NIM analysis.
            client_id: The client identifier for logging.
        """
        if not self._token:
            return  # No token configured, skip silently

        # Tag based on outcome
        await self._tag_contact_by_outcome(contact_id, analysis, client_id)

        # Write summary to custom field
        await self._write_analysis_custom_field(contact_id, analysis, client_id)

    async def _tag_contact_by_outcome(
        self,
        contact_id: str,
        analysis: StructuredOutput,
        client_id: str,
    ) -> None:
        """Add a tag to the contact based on the conversation outcome.

        Mapping:
        - qualified_lead → "qualified"
        - booked → "booked"
        - dropped_off → "dropped-off"
        - not_interested → "not-interested"
        - spam → "spam"
        - unclear → "needs-review"
        """
        tag_map = {
            "qualified_lead": "qualified",
            "booked": "booked",
            "dropped_off": "dropped-off",
            "not_interested": "not-interested",
            "spam": "spam",
            "unclear": "needs-review",
        }

        outcome = analysis.outcome.value
        tag_name = tag_map.get(outcome)
        if not tag_name:
            return

        # First, get or find the tag ID by name
        tag_id = await self._get_tag_id_by_name(tag_name)
        if not tag_id:
            logger.debug(
                "Tag not found in Chatrace, skipping",
                extra={"tag_name": tag_name, "contact_id": contact_id},
            )
            return

        # Apply tag to contact
        try:
            url = f"{CHATRACE_BASE_URL}/contacts/{contact_id}/tags/{tag_id}"
            response = await self._http.post(url, headers=self._headers, params=self._auth_params)

            if response.status_code in (200, 201):
                logger.info(
                    "Tagged contact in Chatrace",
                    extra={
                        "contact_id": contact_id,
                        "tag_name": tag_name,
                        "client_id": client_id,
                    },
                )
            else:
                logger.warning(
                    "Failed to tag contact",
                    extra={
                        "contact_id": contact_id,
                        "tag_name": tag_name,
                        "status_code": response.status_code,
                    },
                )
        except Exception as e:
            logger.warning(
                "Chatrace tag API call failed",
                extra={"contact_id": contact_id, "error": str(e)},
            )

    async def _write_analysis_custom_field(
        self,
        contact_id: str,
        analysis: StructuredOutput,
        client_id: str,
    ) -> None:
        """Write the analysis summary to a custom field on the contact.

        Writes a compact analysis string like:
        "outcome: qualified_lead | sentiment: positive | errors: no | summary: Lead interested in PT"
        """
        analysis_text = (
            f"outcome: {analysis.outcome.value} | "
            f"sentiment: {analysis.sentiment.value} | "
            f"errors: {'yes' if analysis.bot_error_detected else 'no'}"
        )
        if analysis.drop_off_stage:
            analysis_text += f" | dropoff: {analysis.drop_off_stage.value}"
        if analysis.summary:
            analysis_text += f" | {analysis.summary}"

        # Find the "AI Analysis" custom field ID
        field_id = await self._get_custom_field_id_by_name("AI Analysis")
        if not field_id:
            logger.debug(
                "AI Analysis custom field not found in Chatrace, skipping",
                extra={"contact_id": contact_id},
            )
            return

        try:
            url = f"{CHATRACE_BASE_URL}/contacts/{contact_id}/custom_fields/{field_id}"
            response = await self._http.post(
                url,
                headers=self._headers, params=self._auth_params,
                json={"value": analysis_text[:500]},  # Chatrace may have length limits
            )

            if response.status_code in (200, 201):
                logger.info(
                    "Wrote analysis to Chatrace custom field",
                    extra={"contact_id": contact_id, "client_id": client_id},
                )
            else:
                logger.debug(
                    "Failed to write custom field",
                    extra={
                        "contact_id": contact_id,
                        "status_code": response.status_code,
                    },
                )
        except Exception as e:
            logger.warning(
                "Chatrace custom field API call failed",
                extra={"contact_id": contact_id, "error": str(e)},
            )

    async def _get_tag_id_by_name(self, tag_name: str) -> str | None:
        """Look up a tag ID by name from Chatrace.

        Returns:
            The tag ID string, or None if not found.
        """
        try:
            url = f"{CHATRACE_BASE_URL}/accounts/tags/name/{tag_name}"
            response = await self._http.get(url, headers=self._headers, params=self._auth_params)
            if response.status_code == 200:
                data = response.json()
                return str(data.get("id", "")) if data else None
        except Exception as e:
            logger.debug(
                "Failed to look up tag",
                extra={"tag_name": tag_name, "error": str(e)},
            )
        return None

    async def _get_custom_field_id_by_name(self, field_name: str) -> str | None:
        """Look up a custom field ID by name from Chatrace.

        Returns:
            The custom field ID string, or None if not found.
        """
        try:
            url = f"{CHATRACE_BASE_URL}/accounts/custom_fields/name/{field_name}"
            response = await self._http.get(url, headers=self._headers, params=self._auth_params)
            if response.status_code == 200:
                data = response.json()
                return str(data.get("id", "")) if data else None
        except Exception as e:
            logger.debug(
                "Failed to look up custom field",
                extra={"field_name": field_name, "error": str(e)},
            )
        return None
