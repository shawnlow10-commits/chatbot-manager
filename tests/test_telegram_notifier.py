"""Unit tests for the Telegram Notifier module."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from chatbot_monitor.models import AnomalyAlert, DigestMessage, DigestSection
from chatbot_monitor.telegram_notifier import (
    MAX_BULLET_LENGTH,
    MAX_BULLETS_PER_CLIENT,
    TELEGRAM_MAX_LENGTH,
    TelegramNotifier,
)


@pytest.fixture
def bot_token():
    return "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


@pytest.fixture
def chat_id():
    return "-1001234567890"


@pytest.fixture
def notifier(bot_token, chat_id):
    client = httpx.AsyncClient()
    return TelegramNotifier(bot_token, chat_id, client)


@pytest.fixture
def sample_alert():
    return AnomalyAlert(
        client_id="bot_realestate",
        client_display_name="Real Estate Bot",
        issue_type="high_dropoff",
        stage="qualification",
        metric_value=78.5,
        baseline_value=45.0,
        message="Drop-off rate at qualification stage has exceeded threshold.",
    )


@pytest.fixture
def sample_digest():
    return DigestMessage(
        sections=[
            DigestSection(
                client_id="bot_realestate",
                client_display_name="Real Estate Bot",
                bullets=[
                    "Lead volume up 15% vs last week",
                    "Qualification drop-off rate stable at 32%",
                    "3 conversations flagged for bot errors",
                ],
            ),
            DigestSection(
                client_id="bot_insurance",
                client_display_name="Insurance Bot",
                bullets=[
                    "Sentiment trending positive this week",
                    "No anomalies detected",
                ],
            ),
        ],
        generated_at=datetime.now(timezone.utc),
    )


class TestFormatAlertMessage:
    """Tests for alert message formatting."""

    def test_basic_format(self, notifier, sample_alert):
        message = notifier.format_alert_message(sample_alert)

        assert message.startswith("🚨")
        assert "high_dropoff" in message
        assert "Real Estate Bot" in message
        assert "78.5" in message
        assert "45.0" in message
        assert "Drop-off rate" in message

    def test_within_length_limit(self, notifier, sample_alert):
        message = notifier.format_alert_message(sample_alert)
        assert len(message) <= TELEGRAM_MAX_LENGTH

    def test_truncation_preserves_header(self, notifier):
        """Alert with very long message gets truncated but keeps prefix and client."""
        long_alert = AnomalyAlert(
            client_id="bot_test",
            client_display_name="Test Bot",
            issue_type="consecutive_errors",
            stage=None,
            metric_value=5.0,
            baseline_value=3.0,
            message="x" * 5000,  # Exceeds 4096
        )
        message = notifier.format_alert_message(long_alert)

        assert len(message) <= TELEGRAM_MAX_LENGTH
        assert message.startswith("🚨")
        assert "consecutive_errors" in message
        assert "Test Bot" in message

    def test_truncation_indicator(self, notifier):
        """Truncated messages end with an ellipsis indicator."""
        long_alert = AnomalyAlert(
            client_id="bot_test",
            client_display_name="Test Bot",
            issue_type="low_volume",
            stage=None,
            metric_value=2.0,
            baseline_value=10.0,
            message="A" * 5000,
        )
        message = notifier.format_alert_message(long_alert)
        assert "..." in message


class TestFormatDigestMessages:
    """Tests for digest message formatting and splitting."""

    def test_basic_format(self, notifier, sample_digest):
        messages = notifier.format_digest_messages(sample_digest)

        assert len(messages) >= 1
        assert messages[0].startswith("📊")

    def test_client_sections_present(self, notifier, sample_digest):
        messages = notifier.format_digest_messages(sample_digest)
        full_text = "\n".join(messages)

        assert "Real Estate Bot" in full_text
        assert "Insurance Bot" in full_text

    def test_bullet_formatting(self, notifier, sample_digest):
        messages = notifier.format_digest_messages(sample_digest)
        full_text = "\n".join(messages)

        assert "• Lead volume up 15% vs last week" in full_text
        assert "• Sentiment trending positive this week" in full_text

    def test_single_message_within_limit(self, notifier, sample_digest):
        messages = notifier.format_digest_messages(sample_digest)
        for msg in messages:
            assert len(msg) <= TELEGRAM_MAX_LENGTH

    def test_empty_digest(self, notifier):
        digest = DigestMessage(
            sections=[],
            generated_at=datetime.now(timezone.utc),
        )
        messages = notifier.format_digest_messages(digest)
        assert len(messages) == 1
        assert "📊" in messages[0]

    def test_long_bullets_truncated(self, notifier):
        """Bullets exceeding 280 chars get truncated."""
        digest = DigestMessage(
            sections=[
                DigestSection(
                    client_id="bot_test",
                    client_display_name="Test Bot",
                    bullets=["A" * 300],  # Exceeds 280
                ),
            ],
            generated_at=datetime.now(timezone.utc),
        )
        messages = notifier.format_digest_messages(digest)
        full_text = "\n".join(messages)

        # Each bullet line starts with "• " (2 chars) + content
        for line in full_text.split("\n"):
            if line.startswith("• "):
                bullet_content = line[2:]  # Remove "• "
                assert len(bullet_content) <= MAX_BULLET_LENGTH

    def test_max_bullets_per_client(self, notifier):
        """Only first 20 bullets per client are included."""
        digest = DigestMessage(
            sections=[
                DigestSection(
                    client_id="bot_test",
                    client_display_name="Test Bot",
                    bullets=[f"Bullet {i}" for i in range(30)],
                ),
            ],
            generated_at=datetime.now(timezone.utc),
        )
        messages = notifier.format_digest_messages(digest)
        full_text = "\n".join(messages)

        bullet_count = full_text.count("• ")
        assert bullet_count == MAX_BULLETS_PER_CLIENT

    def test_splitting_large_digest(self, notifier):
        """Digest exceeding 4096 chars is split into multiple messages."""
        # Create a digest that will definitely exceed 4096 chars
        sections = []
        for i in range(10):
            sections.append(
                DigestSection(
                    client_id=f"bot_{i}",
                    client_display_name=f"Client Bot {i} with a longer name",
                    bullets=[f"Bullet point number {j} with enough text to take up space: {'x' * 100}" for j in range(15)],
                )
            )

        digest = DigestMessage(
            sections=sections,
            generated_at=datetime.now(timezone.utc),
        )
        messages = notifier.format_digest_messages(digest)

        # Should be multiple messages
        assert len(messages) > 1
        # Each message respects limit
        for msg in messages:
            assert len(msg) <= TELEGRAM_MAX_LENGTH

    def test_splitting_preserves_all_content(self, notifier):
        """Split messages preserve all bullet content."""
        bullets = [f"Important fact {i}" for i in range(20)]
        sections = []
        for i in range(8):
            sections.append(
                DigestSection(
                    client_id=f"bot_{i}",
                    client_display_name=f"Bot {i}",
                    bullets=[f"Point {j} for bot {i}: {'y' * 80}" for j in range(15)],
                )
            )

        digest = DigestMessage(
            sections=sections,
            generated_at=datetime.now(timezone.utc),
        )
        messages = notifier.format_digest_messages(digest)
        full_text = "\n".join(messages)

        # All client names should appear
        for i in range(8):
            assert f"Bot {i}" in full_text


class TestSendAlert:
    """Tests for send_alert with mocked Telegram API."""

    @pytest.mark.asyncio
    async def test_successful_delivery(self, bot_token, chat_id, sample_alert):
        with respx.mock:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            respx.post(api_url).mock(return_value=httpx.Response(200, json={"ok": True}))

            async with httpx.AsyncClient() as client:
                notifier = TelegramNotifier(bot_token, chat_id, client)
                result = await notifier.send_alert(sample_alert)

            assert result is True

    @pytest.mark.asyncio
    @patch("chatbot_monitor.telegram_notifier.asyncio.sleep", new_callable=AsyncMock)
    async def test_failure_after_retries(self, mock_sleep, bot_token, chat_id, sample_alert):
        with respx.mock:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            respx.post(api_url).mock(return_value=httpx.Response(500, json={"ok": False}))

            async with httpx.AsyncClient() as client:
                notifier = TelegramNotifier(bot_token, chat_id, client)
                result = await notifier.send_alert(sample_alert)

            assert result is False

    @pytest.mark.asyncio
    @patch("chatbot_monitor.telegram_notifier.asyncio.sleep", new_callable=AsyncMock)
    async def test_retry_then_success(self, mock_sleep, bot_token, chat_id, sample_alert):
        """Succeeds on the second attempt after initial failure."""
        with respx.mock:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            route = respx.post(api_url).mock(
                side_effect=[
                    httpx.Response(429, json={"ok": False}),
                    httpx.Response(200, json={"ok": True}),
                ]
            )

            async with httpx.AsyncClient() as client:
                notifier = TelegramNotifier(bot_token, chat_id, client)
                result = await notifier.send_alert(sample_alert)

            assert result is True
            assert route.call_count == 2


class TestSendDigest:
    """Tests for send_digest with mocked Telegram API."""

    @pytest.mark.asyncio
    async def test_successful_delivery(self, bot_token, chat_id, sample_digest):
        with respx.mock:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            respx.post(api_url).mock(return_value=httpx.Response(200, json={"ok": True}))

            async with httpx.AsyncClient() as client:
                notifier = TelegramNotifier(bot_token, chat_id, client)
                result = await notifier.send_digest(sample_digest)

            assert result is True

    @pytest.mark.asyncio
    @patch("chatbot_monitor.telegram_notifier.asyncio.sleep", new_callable=AsyncMock)
    async def test_partial_failure(self, mock_sleep, bot_token, chat_id):
        """If one split message fails, returns False but continues sending others."""
        # Create a large digest that will split
        sections = []
        for i in range(10):
            sections.append(
                DigestSection(
                    client_id=f"bot_{i}",
                    client_display_name=f"Client {i}",
                    bullets=[f"Bullet {j}: {'z' * 100}" for j in range(15)],
                )
            )
        digest = DigestMessage(sections=sections, generated_at=datetime.now(timezone.utc))

        with respx.mock:
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            # First message succeeds, rest fail
            respx.post(api_url).mock(
                side_effect=[
                    httpx.Response(200, json={"ok": True}),
                ] + [httpx.Response(500, json={"ok": False})] * 50  # enough for retries
            )

            async with httpx.AsyncClient() as client:
                notifier = TelegramNotifier(bot_token, chat_id, client)
                result = await notifier.send_digest(digest)

            assert result is False
