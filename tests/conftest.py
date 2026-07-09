"""Shared test fixtures for the chatbot monitor test suite.

All fixtures here are opt-in (not autouse) to avoid conflicts with
test files that define their own local fixtures. Tests that need these
can simply accept them as parameters.
"""

import pytest
import pytest_asyncio
import httpx
import respx

from chatbot_monitor.config import AppConfig, ClientConfig
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import ActiveHours, AlertThresholds


# ─── In-Memory MemoryStore ────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def memory_store():
    """Create an in-memory SQLite MemoryStore for testing.

    Initializes the database schema and yields the store.
    Closes the connection after the test completes.
    """
    store = MemoryStore(":memory:")
    await store.initialize()
    yield store
    await store.close()


# ─── Valid AppConfig ──────────────────────────────────────────────────────────


@pytest.fixture
def valid_app_config() -> AppConfig:
    """Provide a fully populated AppConfig suitable for testing.

    Includes one client (bot_test) with active hours and default thresholds.
    """
    default_thresholds = AlertThresholds(
        dropoff_rate_pct=50.0,
        low_volume_pct=50.0,
        consecutive_errors=3,
        consecutive_neg_sentiment=3,
        persistence_count=3,
        cooldown_minutes=60,
    )
    return AppConfig(
        webhook_secret="test-secret-123",
        nim_api_key="test-nim-api-key",
        nim_base_url="https://integrate.api.nvidia.com/v1",
        nim_model="meta/llama-3.1-70b-instruct",
        telegram_bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        telegram_chat_id="-1001234567890",
        digest_schedule="0 8 * * *",
        alert_defaults=default_thresholds,
        clients={
            "bot_test": ClientConfig(
                client_id="bot_test",
                display_name="Test Bot",
                thresholds=default_thresholds,
                active_hours=ActiveHours(
                    start_time="08:00",
                    end_time="22:00",
                    timezone="UTC",
                    days=[0, 1, 2, 3, 4],
                ),
            ),
        },
        db_path=":memory:",
    )


# ─── Mock NIM API (via respx) ────────────────────────────────────────────────


@pytest.fixture
def mock_nim_api():
    """Mock NVIDIA NIM API responses using respx.

    Yields a respx mock router scoped to the NIM base URL.
    Tests can configure specific response patterns via the router.

    Example usage in test:
        def test_nim_call(mock_nim_api):
            mock_nim_api.post("/chat/completions").mock(
                return_value=httpx.Response(200, json={...})
            )
    """
    with respx.mock(base_url="https://integrate.api.nvidia.com/v1") as nim_mock:
        yield nim_mock


# ─── Mock Telegram Bot API (via respx) ───────────────────────────────────────


@pytest.fixture
def mock_telegram_api():
    """Mock Telegram Bot API responses using respx.

    Yields a respx mock router scoped to the Telegram API base URL.
    Tests can configure specific response patterns via the router.

    Example usage in test:
        def test_telegram_send(mock_telegram_api):
            mock_telegram_api.post(
                "/bot123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11/sendMessage"
            ).mock(return_value=httpx.Response(200, json={"ok": True}))
    """
    with respx.mock(base_url="https://api.telegram.org") as telegram_mock:
        yield telegram_mock


# ─── Async HTTP Client ────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def async_http_client():
    """Provide an async httpx client for tests that need real HTTP interactions.

    The client is properly closed after the test completes.
    """
    async with httpx.AsyncClient() as client:
        yield client
