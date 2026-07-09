"""Unit tests for the webhook receiver endpoint."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from chatbot_monitor.config import AppConfig, ClientConfig
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import AlertThresholds
from chatbot_monitor.receiver import router


def _make_app(config: AppConfig, store: MemoryStore) -> FastAPI:
    """Create a test FastAPI app with receiver router and injected dependencies."""
    app = FastAPI()
    app.state.config = config
    app.state.store = store
    app.state.analyzer = AsyncMock()
    app.state.detector = AsyncMock(return_value=[])
    app.state.notifier = AsyncMock()
    app.include_router(router)
    return app


def _make_config(
    webhook_secret: str = "test-secret-123",
    clients: dict | None = None,
) -> AppConfig:
    """Create a test AppConfig."""
    default_thresholds = AlertThresholds()
    if clients is None:
        clients = {
            "bot_test": ClientConfig(
                client_id="bot_test",
                display_name="Test Bot",
                thresholds=default_thresholds,
                active_hours=None,
            )
        }
    return AppConfig(
        webhook_secret=webhook_secret,
        nim_api_key="test-nim-key",
        nim_base_url="https://nim.test/v1",
        nim_model="test-model",
        telegram_bot_token="test-bot-token",
        telegram_chat_id="123456",
        digest_schedule="0 8 * * *",
        alert_defaults=default_thresholds,
        clients=clients,
        db_path=":memory:",
    )


def _make_store() -> MemoryStore:
    """Create a mock MemoryStore."""
    store = AsyncMock(spec=MemoryStore)
    store.has_dedupe_key = AsyncMock(return_value=False)
    store.store_dedupe_key = AsyncMock()
    store.store_raw_payload = AsyncMock()
    return store


def _valid_payload() -> dict:
    """Create a valid webhook payload."""
    return {
        "contact_id": "contact_123",
        "timestamp": "2024-01-15T10:30:00Z",
        "chat_history": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
    }


class TestWebhookAuthentication:
    """Tests for X-Webhook-Secret header validation."""

    def test_missing_secret_returns_401(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
        )
        assert response.status_code == 401

    def test_wrong_secret_returns_401(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "wrong-secret"},
        )
        assert response.status_code == 401

    def test_correct_secret_passes_auth(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200


class TestClientValidation:
    """Tests for client_id validation."""

    def test_unknown_client_returns_404(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/unknown_bot",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 404

    def test_known_client_passes(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200


class TestPayloadSizeLimit:
    """Tests for Content-Length ≤ 1MB check."""

    def test_oversized_content_length_returns_413(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        # Send a request with Content-Length header indicating > 1MB
        response = client.post(
            "/webhook/bot_test",
            content=b'{"test": "data"}',
            headers={
                "X-Webhook-Secret": "test-secret-123",
                "Content-Length": "2000000",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 413

    def test_normal_size_payload_passes(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200


class TestPayloadValidation:
    """Tests for payload validation (missing fields, etc.)."""

    def test_missing_contact_id_returns_200_with_skip(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        payload = {
            "timestamp": "2024-01-15T10:30:00Z",
            "chat_history": [{"role": "user", "content": "Hello"}],
        }
        response = client.post(
            "/webhook/bot_test",
            json=payload,
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped"
        assert data["reason"] == "validation_error"

    def test_missing_timestamp_returns_200_with_skip(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        payload = {
            "contact_id": "contact_123",
            "chat_history": [{"role": "user", "content": "Hello"}],
        }
        response = client.post(
            "/webhook/bot_test",
            json=payload,
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped"

    def test_empty_chat_history_returns_200_with_skip(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        payload = {
            "contact_id": "contact_123",
            "timestamp": "2024-01-15T10:30:00Z",
            "chat_history": [],
        }
        response = client.post(
            "/webhook/bot_test",
            json=payload,
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped"

    def test_invalid_json_returns_200_with_skip(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            content=b"not valid json{{{",
            headers={
                "X-Webhook-Secret": "test-secret-123",
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped"


class TestDeduplication:
    """Tests for dedupe_key duplicate checking."""

    def test_duplicate_payload_returns_200_with_skip(self):
        config = _make_config()
        store = _make_store()
        store.has_dedupe_key = AsyncMock(return_value=True)
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "skipped"
        assert data["reason"] == "duplicate"

    def test_new_payload_stores_dedupe_key_and_raw(self):
        config = _make_config()
        store = _make_store()
        store.has_dedupe_key = AsyncMock(return_value=False)
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert "dedupe_key" in data

        # Verify store methods were called
        store.store_dedupe_key.assert_called_once()
        store.store_raw_payload.assert_called_once()


class TestSuccessfulWebhook:
    """Tests for the successful webhook acceptance flow."""

    def test_valid_webhook_returns_200_with_accepted(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "accepted"
        assert "dedupe_key" in data
        # Dedupe key should be a 64-char hex string (SHA-256)
        assert len(data["dedupe_key"]) == 64

    def test_response_includes_correct_content_type(self):
        config = _make_config()
        store = _make_store()
        app = _make_app(config, store)
        client = TestClient(app)

        response = client.post(
            "/webhook/bot_test",
            json=_valid_payload(),
            headers={"X-Webhook-Secret": "test-secret-123"},
        )
        assert response.headers["content-type"] == "application/json"
