"""FastAPI application factory and lifespan management.

Creates the FastAPI app with:
- Lifespan context: loads config, initializes MemoryStore, creates shared httpx client,
  instantiates NIMAnalyzer, AnomalyDetector, TelegramNotifier, DigestScheduler
- Dependency injection via app.state (get_config, get_store, etc.)
- Graceful startup/shutdown of DigestScheduler and shared resources
- Structured JSON logging setup on startup

Run with: uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port 8000
"""

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request

from chatbot_monitor.anomaly_detector import AnomalyDetector
from chatbot_monitor.config import AppConfig, load_config
from chatbot_monitor.digest_scheduler import DigestScheduler
from chatbot_monitor.logging_config import get_logger, setup_logging
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.nim_analyzer import NIMAnalyzer
from chatbot_monitor.receiver import router as receiver_router
from chatbot_monitor.telegram_bot import bot_router
from chatbot_monitor.telegram_notifier import TelegramNotifier

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager.

    Startup:
        1. Set up structured logging
        2. Load configuration from config.yaml + env vars
        3. Initialize MemoryStore (SQLite with WAL mode)
        4. Create shared httpx.AsyncClient for NIM and Telegram APIs
        5. Instantiate NIMAnalyzer, TelegramNotifier, AnomalyDetector, DigestScheduler
        6. Start DigestScheduler (registers APScheduler cron jobs)
        7. Inject all components into app.state for dependency injection

    Shutdown:
        1. Stop DigestScheduler gracefully
        2. Close shared httpx.AsyncClient
        3. Close MemoryStore (SQLite connection)
    """
    # --- Startup ---
    setup_logging()
    logger.info("Starting Chatbot Monitor service")

    # Load configuration
    config = load_config()
    logger.info(
        "Configuration loaded",
        extra={"clients_count": len(config.clients), "db_path": config.db_path},
    )

    # Initialize memory store (ensure directory exists)
    import os
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_KEY", "")

    if supabase_url and supabase_key:
        # Use persistent Supabase storage
        from chatbot_monitor.supabase_store import SupabaseStore
        store = SupabaseStore(supabase_url, supabase_key)
        await store.initialize()
        logger.info("Using Supabase persistent storage")
    else:
        # Fallback to SQLite (ephemeral on Render)
        db_dir = os.path.dirname(config.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        store = MemoryStore(config.db_path)
        await store.initialize()
        logger.info("Using SQLite storage (ephemeral)", extra={"db_path": config.db_path})

    # Create shared HTTP client
    http_client = httpx.AsyncClient()

    # Instantiate core components
    analyzer = NIMAnalyzer(config, http_client)
    notifier = TelegramNotifier(
        config.telegram_bot_token, config.telegram_chat_id, http_client
    )
    detector = AnomalyDetector(config, store, notifier)
    scheduler = DigestScheduler(
        config, store, analyzer, notifier, anomaly_detector=detector
    )

    # Start scheduler (registers digest, inactive-hours, and purge cron jobs)
    scheduler.start()
    logger.info("DigestScheduler started")

    # Initialize Chatrace API client (optional — only if token is configured)
    chatrace_client = None
    chatrace_token = os.environ.get("CHATRACE_API_TOKEN", "")
    if chatrace_token:
        from chatbot_monitor.chatrace_api import ChatraceClient
        chatrace_client = ChatraceClient(chatrace_token, http_client)
        logger.info("Chatrace API client initialized")

        # Run bulk sync on startup to repopulate DB after restart
        try:
            for cid in config.clients:
                synced = await chatrace_client.bulk_sync_contacts(store, analyzer, cid)
                if synced > 0:
                    logger.info(f"Startup sync: {synced} contacts for {cid}")
        except Exception as e:
            logger.warning(f"Startup sync failed (non-blocking): {e}")

    # Inject into app state for dependency injection
    app.state.config = config
    app.state.store = store
    app.state.analyzer = analyzer
    app.state.detector = detector
    app.state.notifier = notifier
    app.state.scheduler = scheduler
    app.state.http_client = http_client
    app.state.chatrace_client = chatrace_client

    yield

    # --- Shutdown ---
    logger.info("Shutting down Chatbot Monitor service")

    scheduler.shutdown()
    await http_client.aclose()
    await store.close()

    logger.info("Shutdown complete")


# --- Dependency injection helpers ---


def get_config(request: Request) -> AppConfig:
    """Retrieve AppConfig from app state."""
    return request.app.state.config


def get_store(request: Request) -> MemoryStore:
    """Retrieve MemoryStore from app state."""
    return request.app.state.store


# --- Application factory ---

app = FastAPI(
    title="Chatbot Monitor",
    description="Conversation Intelligence Monitor - webhook receiver, NIM analysis, anomaly detection, and Telegram alerts",
    version="1.0.0",
    lifespan=lifespan,
)

# Include routers
app.include_router(receiver_router)
app.include_router(bot_router)
