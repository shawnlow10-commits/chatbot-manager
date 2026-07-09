"""Telegram Bot API client for alert and digest delivery.

Handles message formatting, truncation (4096 char limit), splitting for long
digests, and exponential backoff retry on API errors.
"""

import asyncio
import logging

import httpx

from chatbot_monitor.logging_config import get_logger
from chatbot_monitor.models import AnomalyAlert, DigestMessage

logger = get_logger("telegram_notifier")

# Telegram message character limit
TELEGRAM_MAX_LENGTH = 4096

# Retry configuration
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2

# Digest formatting limits
MAX_BULLETS_PER_CLIENT = 20
MAX_BULLET_LENGTH = 280


class TelegramNotifier:
    """Sends alerts and digests via the Telegram Bot API.

    Uses httpx for async HTTP requests. Implements exponential backoff retry
    on Telegram API errors (HTTP 4xx/5xx).
    """

    def __init__(self, bot_token: str, chat_id: str, http_client: httpx.AsyncClient):
        """Initialize the Telegram notifier.

        Args:
            bot_token: Telegram Bot API token.
            chat_id: Target Telegram chat ID for messages.
            http_client: Shared httpx async client for making requests.
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.http_client = http_client
        self._api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async def send_alert(self, alert: AnomalyAlert) -> bool:
        """Send a 🚨-prefixed alert message to Telegram.

        Formats the alert, truncates to 4096 chars if needed, and retries
        up to 3 times with exponential backoff on API errors.

        Args:
            alert: The anomaly alert to send.

        Returns:
            True if the message was delivered successfully, False otherwise.
        """
        message = self.format_alert_message(alert)
        success = await self._send_message_with_retry(message)

        if success:
            logger.info(
                "Alert delivered",
                extra={
                    "client_id": alert.client_id,
                    "issue_type": alert.issue_type,
                    "delivery_status": "success",
                },
            )
        else:
            logger.error(
                "Alert delivery failed after all retries",
                extra={
                    "client_id": alert.client_id,
                    "issue_type": alert.issue_type,
                    "delivery_status": "failed",
                },
            )

        return success

    async def send_digest(self, digest: DigestMessage) -> bool:
        """Send a 📊-prefixed digest message to Telegram.

        Formats the digest with one section per client, splits into multiple
        messages if the total exceeds 4096 chars, and retries each message
        up to 3 times with exponential backoff on API errors.

        Args:
            digest: The digest message containing all client sections.

        Returns:
            True if all messages were delivered successfully, False otherwise.
        """
        messages = self.format_digest_messages(digest)
        all_success = True

        for i, message in enumerate(messages):
            success = await self._send_message_with_retry(message)
            if not success:
                all_success = False
                logger.error(
                    "Digest delivery failed",
                    extra={
                        "message_part": i + 1,
                        "total_parts": len(messages),
                        "delivery_status": "failed",
                    },
                )
                # Continue trying to send remaining parts
            else:
                logger.info(
                    "Digest part delivered",
                    extra={
                        "message_part": i + 1,
                        "total_parts": len(messages),
                        "delivery_status": "success",
                    },
                )

        return all_success

    def format_alert_message(self, alert: AnomalyAlert) -> str:
        """Format an alert into a Telegram message with 🚨 prefix.

        If the formatted message exceeds 4096 characters, it is truncated
        while preserving the prefix, issue_type, and client display_name.

        Args:
            alert: The anomaly alert to format.

        Returns:
            Formatted message string, at most 4096 characters.
        """
        message = (
            f"🚨 {alert.issue_type}\n\n"
            f"Client: {alert.client_display_name}\n\n"
            f"{alert.message}\n\n"
            f"Metric: {alert.metric_value} (baseline: {alert.baseline_value})"
        )

        if len(message) <= TELEGRAM_MAX_LENGTH:
            return message

        # Truncate while preserving prefix, issue_type, and client name
        return self._truncate_alert(alert, message)

    def format_digest_messages(self, digest: DigestMessage) -> list[str]:
        """Format a digest into one or more Telegram messages with 📊 prefix.

        Each message is at most 4096 characters. If the full digest exceeds
        the limit, it is split into multiple messages with no data loss.

        Args:
            digest: The digest message containing client sections.

        Returns:
            List of formatted message strings, each ≤ 4096 characters.
        """
        header = "📊 Daily Digest\n"
        sections = self._format_sections(digest)

        if not sections:
            return [header.rstrip("\n")]

        # Try to fit everything in one message
        full_message = header + "\n" + "\n".join(sections)
        if len(full_message) <= TELEGRAM_MAX_LENGTH:
            return [full_message]

        # Split into multiple messages
        return self._split_digest_messages(header, sections)

    def _truncate_alert(self, alert: AnomalyAlert, message: str) -> str:
        """Truncate an alert message to fit within 4096 chars.

        Preserves the 🚨 prefix, issue_type, and client display_name.
        The message body is truncated with a '...' indicator.
        """
        # Build the preserved header
        preserved_header = (
            f"🚨 {alert.issue_type}\n\n"
            f"Client: {alert.client_display_name}\n\n"
        )

        # Build the footer with metric info
        footer = f"\n\nMetric: {alert.metric_value} (baseline: {alert.baseline_value})"

        # Calculate available space for the message body
        truncation_indicator = "..."
        available = TELEGRAM_MAX_LENGTH - len(preserved_header) - len(footer) - len(truncation_indicator)

        if available <= 0:
            # Even header + footer exceeds limit; truncate footer too
            available_for_all = TELEGRAM_MAX_LENGTH - len(preserved_header) - len(truncation_indicator)
            if available_for_all <= 0:
                return preserved_header[:TELEGRAM_MAX_LENGTH]
            return preserved_header + alert.message[:available_for_all] + truncation_indicator

        truncated_body = alert.message[:available] + truncation_indicator
        return preserved_header + truncated_body + footer

    def _format_sections(self, digest: DigestMessage) -> list[str]:
        """Format each client section as a string block.

        Enforces max 20 bullets per client, max 280 chars per bullet.
        """
        sections = []
        for section in digest.sections:
            section_header = f"--- {section.client_display_name} ---"
            bullets = section.bullets[:MAX_BULLETS_PER_CLIENT]

            formatted_bullets = []
            for bullet in bullets:
                if len(bullet) > MAX_BULLET_LENGTH:
                    bullet = bullet[: MAX_BULLET_LENGTH - 3] + "..."
                formatted_bullets.append(f"• {bullet}")

            section_text = section_header + "\n" + "\n".join(formatted_bullets)
            sections.append(section_text)

        return sections

    def _split_digest_messages(self, header: str, sections: list[str]) -> list[str]:
        """Split digest content into multiple messages, each ≤ 4096 chars.

        The first message starts with the header (📊 prefix). Subsequent
        messages start with a continuation indicator. All bullet content
        is preserved across the split with no data loss.
        """
        messages: list[str] = []
        current_message = header

        for section in sections:
            # Check if adding this section would exceed the limit
            separator = "\n\n" if current_message != header else "\n"
            candidate = current_message + separator + section

            if len(candidate) <= TELEGRAM_MAX_LENGTH:
                current_message = candidate
            else:
                # Try to fit the section by splitting it line by line
                lines = section.split("\n")
                section_header_line = lines[0] if lines else ""
                bullet_lines = lines[1:] if len(lines) > 1 else []

                # Check if we can at least add the section header to current message
                section_with_header = current_message + separator + section_header_line
                if len(section_with_header) <= TELEGRAM_MAX_LENGTH and bullet_lines:
                    # Add section header, then try to fit bullets
                    current_message = section_with_header
                    for bullet_line in bullet_lines:
                        bullet_candidate = current_message + "\n" + bullet_line
                        if len(bullet_candidate) <= TELEGRAM_MAX_LENGTH:
                            current_message = bullet_candidate
                        else:
                            # Start a new message
                            if current_message.strip():
                                messages.append(current_message)
                            current_message = "📊 (continued)\n\n" + section_header_line + "\n" + bullet_line
                            # If even this exceeds limit, force push
                            if len(current_message) > TELEGRAM_MAX_LENGTH:
                                messages.append(current_message[:TELEGRAM_MAX_LENGTH])
                                current_message = "📊 (continued)\n"
                else:
                    # Current message is full; start a new message with this section
                    if current_message.strip():
                        messages.append(current_message)

                    # Start new message with continuation header
                    new_message = "📊 (continued)\n\n" + section
                    if len(new_message) <= TELEGRAM_MAX_LENGTH:
                        current_message = new_message
                    else:
                        # Section itself is too large; split it line by line
                        current_message = "📊 (continued)\n\n" + section_header_line
                        for bullet_line in bullet_lines:
                            bullet_candidate = current_message + "\n" + bullet_line
                            if len(bullet_candidate) <= TELEGRAM_MAX_LENGTH:
                                current_message = bullet_candidate
                            else:
                                messages.append(current_message)
                                current_message = "📊 (continued)\n\n" + section_header_line + "\n" + bullet_line
                                if len(current_message) > TELEGRAM_MAX_LENGTH:
                                    messages.append(current_message[:TELEGRAM_MAX_LENGTH])
                                    current_message = "📊 (continued)\n"

        # Don't forget the last message
        if current_message.strip():
            messages.append(current_message)

        return messages if messages else [header.rstrip("\n")]

    async def _send_message_with_retry(self, text: str) -> bool:
        """Send a message to Telegram with exponential backoff retry.

        Retries up to 3 times with delays of 2s → 4s → 8s on HTTP 4xx/5xx
        errors from the Telegram API.

        Args:
            text: The message text to send (must be ≤ 4096 chars).

        Returns:
            True if the message was sent successfully, False if all retries exhausted.
        """
        payload = {
            "chat_id": self.chat_id,
            "text": text,
        }

        for attempt in range(MAX_RETRIES):
            try:
                response = await self.http_client.post(self._api_url, json=payload)

                if response.status_code == 200:
                    return True

                # HTTP error from Telegram API
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Telegram API error, retrying",
                    extra={
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                        "max_retries": MAX_RETRIES,
                        "backoff_seconds": backoff,
                    },
                )

                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(backoff)

            except httpx.HTTPError as exc:
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** attempt)
                logger.warning(
                    "Telegram API request error, retrying",
                    extra={
                        "error": str(exc),
                        "attempt": attempt + 1,
                        "max_retries": MAX_RETRIES,
                        "backoff_seconds": backoff,
                    },
                )

                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(backoff)

        # All retries exhausted
        logger.error(
            "Telegram message delivery failed after all retries",
            extra={"max_retries": MAX_RETRIES},
        )
        return False
