"""Webhook receiver endpoint with authentication and background processing.

Exposes POST /webhook/{client_id} that:
1. Validates X-Webhook-Secret header (401 on mismatch/missing)
2. Checks Content-Length ≤ 1MB (413 if exceeded)
3. Validates client_id is in configured clients (404 if unknown)
4. Parses and validates payload (200 with skip on validation error)
5. Computes dedupe_key and checks for duplicates (200 with skip if duplicate)
6. Stores dedupe_key + raw payload, responds 200 immediately
7. Enqueues background task: NIM analysis → persist output → evaluate anomalies → alert
"""

import json

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from chatbot_monitor.config import AppConfig, ClientConfig
from chatbot_monitor.deduplicator import compute_dedupe_key
from chatbot_monitor.logging_config import contact_id_var, get_logger
from chatbot_monitor.memory_store import MemoryStore
from chatbot_monitor.models import StructuredOutput
from chatbot_monitor.validator import ValidationError, validate_payload

logger = get_logger("receiver")

# Maximum request body size: 1MB
MAX_BODY_SIZE = 1_048_576  # 1MB in bytes

router = APIRouter()


def get_config(request: Request) -> AppConfig:
    """Dependency: retrieve AppConfig from app state."""
    return request.app.state.config


def get_store(request: Request) -> MemoryStore:
    """Dependency: retrieve MemoryStore from app state."""
    return request.app.state.store


@router.post("/notify/new-lead/{client_id}")
async def new_lead_endpoint(
    client_id: str,
    request: Request,
    config: AppConfig = Depends(get_config),
) -> Response:
    """POST /notify/new-lead/{client_id} - sends immediate Telegram notification for new contacts.

    Chatrace fires this when a new contact is added. Sends a quick Telegram
    message with the lead's name and phone number.
    """
    # Validate secret
    secret = request.headers.get("X-Webhook-Secret")
    if not secret or secret != config.webhook_secret:
        return Response(
            content=json.dumps({"detail": "Unauthorized"}),
            status_code=401,
            media_type="application/json",
        )

    # Read body
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        body = {}

    # Extract contact info from Chatrace payload
    name = body.get("full_name") or body.get("first_name") or "Unknown"
    phone = body.get("phone") or body.get("id") or "Unknown"
    channel = body.get("channel", "")

    # Get client display name
    client_config = config.clients.get(client_id)
    display_name = client_config.display_name if client_config else client_id

    # Send Telegram notification
    notifier = request.app.state.notifier
    message = (
        f"🆕 New Lead\n\n"
        f"Client: {display_name}\n"
        f"Name: {name}\n"
        f"Phone: {phone}\n"
    )

    try:
        await notifier._send_message_with_retry(message)
        logger.info(
            "New lead notification sent",
            extra={"client_id": client_id, "phone": phone, "name": name},
        )
    except Exception as e:
        logger.error(
            "Failed to send new lead notification",
            extra={"client_id": client_id, "error": str(e)},
        )

    return Response(
        content=json.dumps({"status": "notified"}),
        status_code=200,
        media_type="application/json",
    )


@router.post("/webhook/{client_id}")
async def webhook_endpoint(
    client_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    config: AppConfig = Depends(get_config),
    store: MemoryStore = Depends(get_store),
) -> Response:
    """POST /webhook/{client_id} - validates secret, stores raw, enqueues analysis.

    Returns:
        HTTP 200 on success or graceful skip (validation error, duplicate).
        HTTP 401 if X-Webhook-Secret is missing or doesn't match.
        HTTP 404 if client_id is not in configured clients.
        HTTP 413 if request body exceeds 1MB.
    """
    # Step 1: Validate X-Webhook-Secret header
    secret = request.headers.get("X-Webhook-Secret")
    if not secret or secret != config.webhook_secret:
        logger.warning(
            "Unauthorized webhook request: invalid or missing secret",
            extra={"client_id": client_id},
        )
        return Response(
            content=json.dumps({"detail": "Unauthorized"}),
            status_code=401,
            media_type="application/json",
        )

    # Step 2: Check Content-Length ≤ 1MB
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                logger.warning(
                    "Request body too large",
                    extra={
                        "client_id": client_id,
                        "content_length": int(content_length),
                    },
                )
                return Response(
                    content=json.dumps({"detail": "Payload too large"}),
                    status_code=413,
                    media_type="application/json",
                )
        except (ValueError, TypeError):
            pass

    # Step 3: Validate client_id is in configured clients
    if client_id not in config.clients:
        logger.warning(
            "Unknown client_id in webhook request",
            extra={"client_id": client_id},
        )
        return Response(
            content=json.dumps({"detail": "Unknown client"}),
            status_code=404,
            media_type="application/json",
        )

    # Read the request body
    body_bytes = await request.body()

    # Double-check actual body size (in case Content-Length was absent or wrong)
    if len(body_bytes) > MAX_BODY_SIZE:
        logger.warning(
            "Request body too large (actual size check)",
            extra={"client_id": client_id, "body_size": len(body_bytes)},
        )
        return Response(
            content=json.dumps({"detail": "Payload too large"}),
            status_code=413,
            media_type="application/json",
        )

    # Parse JSON body
    try:
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            "Invalid JSON in webhook payload",
            extra={"client_id": client_id, "error": str(e)},
        )
        return Response(
            content=json.dumps({"detail": "Invalid JSON", "status": "skipped"}),
            status_code=200,
            media_type="application/json",
        )

    # Step 4: Validate payload via validator module
    try:
        validated_payload = validate_payload(client_id, body)
    except ValidationError as e:
        logger.warning(
            "Payload validation failed",
            extra={
                "client_id": client_id,
                "validation_errors": e.errors,
            },
        )
        return Response(
            content=json.dumps({"status": "skipped", "reason": "validation_error"}),
            status_code=200,
            media_type="application/json",
        )

    # Set contact_id context variable for tracing
    contact_id = validated_payload.contact_id
    contact_id_var.set(contact_id)

    # Step 5: Compute dedupe_key
    dedupe_key = compute_dedupe_key(
        client_id, contact_id, validated_payload.timestamp
    )

    # Step 6: Check for duplicates
    if await store.has_dedupe_key(dedupe_key):
        logger.info(
            "Duplicate webhook payload, skipping",
            extra={
                "client_id": client_id,
                "contact_id": contact_id,
                "dedupe_key": dedupe_key,
            },
        )
        return Response(
            content=json.dumps({"status": "skipped", "reason": "duplicate"}),
            status_code=200,
            media_type="application/json",
        )

    # Step 7: Store dedupe_key + raw payload before responding
    await store.store_dedupe_key(dedupe_key, client_id)
    await store.store_raw_payload(
        client_id=client_id,
        dedupe_key=dedupe_key,
        contact_id=contact_id,
        timestamp=validated_payload.timestamp,
        payload=body,
    )

    # Log incoming webhook at INFO level
    logger.info(
        "Webhook received",
        extra={
            "client_id": client_id,
            "contact_id": contact_id,
            "dedupe_key": dedupe_key,
        },
    )

    # Step 8: Respond 200 immediately, enqueue background analysis
    background_tasks.add_task(
        _process_conversation,
        client_id=client_id,
        contact_id=contact_id,
        chatrace_id=validated_payload.chatrace_id,
        dedupe_key=dedupe_key,
        timestamp=validated_payload.timestamp,
        chat_history=[msg.model_dump() for msg in validated_payload.chat_history],
        request=request,
    )

    return Response(
        content=json.dumps({"status": "accepted", "dedupe_key": dedupe_key}),
        status_code=200,
        media_type="application/json",
    )


async def _process_conversation(
    client_id: str,
    contact_id: str,
    chatrace_id: str | None,
    dedupe_key: str,
    timestamp: str,
    chat_history: list[dict],
    request: Request,
) -> None:
    """Background task: analyze conversation and detect anomalies.

    Pipeline:
    1. Call NIMAnalyzer.analyze()
    2. If analysis succeeds, call store.store_structured_output()
    3. Call AnomalyDetector.evaluate()
    4. For each triggered alert, call TelegramNotifier.send_alert()
    """
    # Set contact_id context for log tracing in the background task
    contact_id_var.set(contact_id)

    # Retrieve dependencies from app state
    analyzer = request.app.state.analyzer
    store: MemoryStore = request.app.state.store
    detector = request.app.state.detector

    try:
        # Step 1: Analyze conversation via NIM
        result: StructuredOutput | None = await analyzer.analyze(
            chat_history=chat_history,
            client_id=client_id,
            dedupe_key=dedupe_key,
        )

        if result is None:
            logger.warning(
                "NIM analysis returned no result, skipping further processing",
                extra={
                    "client_id": client_id,
                    "contact_id": contact_id,
                    "dedupe_key": dedupe_key,
                },
            )
            return

        # Step 2: Check for immediate flags (spam, salesperson, notable contact)
        notifier = request.app.state.notifier
        should_flag = False
        flag_reason = ""

        if result.outcome.value == "spam":
            should_flag = True
            flag_reason = "Salesperson/vendor detected"
        elif result.notable_quote and any(
            word in (result.notable_quote or "").lower()
            for word in ["sell", "offer", "partnership", "promote", "pitch", "service", "emergency", "can't move", "accident"]
        ):
            should_flag = True
            flag_reason = "Contact needs human attention"
        elif result.sentiment.value == "negative" and result.bot_error_detected:
            should_flag = True
            flag_reason = "Frustrated user + bot error"

        if should_flag:
            client_config = request.app.state.config.clients.get(client_id)
            display_name = client_config.display_name if client_config else client_id
            flag_message = (
                f"⚠️ Flagged Contact\n\n"
                f"Client: {display_name}\n"
                f"Contact: {contact_id}\n"
                f"Reason: {flag_reason}\n"
                f"Outcome: {result.outcome.value}\n"
                f"Sentiment: {result.sentiment.value}\n"
            )
            if result.notable_quote:
                flag_message += f"Quote: \"{result.notable_quote}\"\n"
            if result.summary:
                flag_message += f"Summary: {result.summary}"

            try:
                await notifier._send_message_with_retry(flag_message)
                logger.info(
                    "Flagged contact notification sent",
                    extra={
                        "client_id": client_id,
                        "contact_id": contact_id,
                        "flag_reason": flag_reason,
                    },
                )
            except Exception as e:
                logger.error(
                    "Failed to send flag notification",
                    extra={"client_id": client_id, "error": str(e)},
                )

        # Step 3: Persist structured output
        try:
            await store.store_structured_output(
                client_id=client_id,
                contact_id=contact_id,
                dedupe_key=dedupe_key,
                timestamp=timestamp,
                output=result,
            )
        except Exception as e:
            logger.error(
                "Failed to persist structured output",
                extra={
                    "client_id": client_id,
                    "contact_id": contact_id,
                    "dedupe_key": dedupe_key,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return

        # Step 3b: Sync analysis back to Chatrace (best-effort, never blocks)
        chatrace_client = getattr(request.app.state, "chatrace_client", None)
        if chatrace_client:
            # Use the Chatrace numeric ID for API calls, fall back to contact_id
            api_contact_id = chatrace_id or contact_id
            try:
                await chatrace_client.sync_analysis_to_contact(
                    contact_id=api_contact_id,
                    analysis=result,
                    client_id=client_id,
                )
            except Exception as e:
                logger.warning(
                    "Chatrace sync failed (non-blocking)",
                    extra={"contact_id": api_contact_id, "error": str(e)},
                )

        # Step 4: Evaluate anomalies
        try:
            alerts = await detector.evaluate(client_id, result)
        except Exception as e:
            logger.error(
                "Anomaly detection failed",
                extra={
                    "client_id": client_id,
                    "contact_id": contact_id,
                    "dedupe_key": dedupe_key,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return

        # Step 5: Send alerts for triggered anomalies
        if alerts:
            notifier = request.app.state.notifier
            for alert in alerts:
                try:
                    await notifier.send_alert(alert)
                    logger.info(
                        "Anomaly alert sent",
                        extra={
                            "client_id": client_id,
                            "contact_id": contact_id,
                            "issue_type": alert.issue_type,
                        },
                    )
                except Exception as e:
                    logger.error(
                        "Failed to send anomaly alert",
                        extra={
                            "client_id": client_id,
                            "contact_id": contact_id,
                            "issue_type": alert.issue_type,
                            "error": str(e),
                            "error_type": type(e).__name__,
                        },
                    )

    except Exception as e:
        logger.error(
            "Unexpected error in background conversation processing",
            extra={
                "client_id": client_id,
                "contact_id": contact_id,
                "dedupe_key": dedupe_key,
                "error": str(e),
                "error_type": type(e).__name__,
                "module": "receiver",
            },
        )
