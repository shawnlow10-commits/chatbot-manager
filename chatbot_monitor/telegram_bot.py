"""Interactive Telegram bot handler for live AI agent commands.

Adds a webhook endpoint that receives messages FROM Telegram (when you
message the bot). Parses commands, queries Chatrace/NIM, and replies.

This module is entirely additive — it registers a new route and doesn't
modify any existing webhook/analysis/alert logic.

Supported commands:
- /check <phone> — Pull contact's chat history, run NIM analysis, report back
- /today — Summary of today's conversations so far
- /dropoffs — List contacts that dropped off today
- /status — Quick system status (conversations today, alerts sent)
- /tag <phone> <tag_name> — Tag a contact in Chatrace
- /help — List available commands
- Any other text — Sent to NIM as a free-form question about your data
"""

import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Request, Response

from chatbot_monitor.logging_config import get_logger

logger = get_logger("telegram_bot")

bot_router = APIRouter()


@bot_router.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> Response:
    """Receive incoming messages from Telegram and respond.

    This endpoint is called by Telegram's webhook system when you send
    a message to your bot.
    """
    try:
        body = await request.json()
    except Exception:
        return Response(content="ok", status_code=200)

    # Extract message details
    message = body.get("message")
    if not message:
        return Response(content="ok", status_code=200)

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "").strip()

    if not text or not chat_id:
        return Response(content="ok", status_code=200)

    # Only respond to messages from the configured chat ID (security)
    config = request.app.state.config
    if chat_id != config.telegram_chat_id:
        logger.warning(
            "Telegram message from unauthorized chat",
            extra={"chat_id": chat_id},
        )
        return Response(content="ok", status_code=200)

    # Route command
    notifier = request.app.state.notifier
    store = request.app.state.store

    try:
        if text.startswith("/check"):
            reply = await _handle_check(text, request)
        elif text.startswith("/today"):
            reply = await _handle_today(store, config)
        elif text.startswith("/dropoffs"):
            reply = await _handle_dropoffs(store, config)
        elif text.startswith("/status"):
            reply = await _handle_status(store, config)
        elif text.startswith("/tag"):
            reply = await _handle_tag(text, request)
        elif text.startswith("/help"):
            reply = _handle_help()
        else:
            reply = await _handle_freeform(text, request)
    except Exception as e:
        logger.error("Command handler error", extra={"error": str(e), "command": text})
        reply = f"❌ Error: {str(e)[:200]}"

    # Send reply
    await notifier._send_message_with_retry(reply)

    return Response(content="ok", status_code=200)


def _handle_help() -> str:
    """Return the help text listing available commands."""
    return (
        "🤖 Available commands:\n\n"
        "/check <phone> — Analyze a contact's conversation\n"
        "/today — Today's conversation summary\n"
        "/dropoffs — Who dropped off today\n"
        "/status — System status\n"
        "/tag <phone> <tag> — Tag a contact\n"
        "/help — This message\n\n"
        "Or just type a question and I'll try to answer it."
    )


async def _handle_check(text: str, request: Request) -> str:
    """Handle /check <phone> — pull contact chat and analyze."""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: /check <phone or contact_id>\nExample: /check +601119801333"

    contact_id = parts[1].strip()
    store = request.app.state.store
    config = request.app.state.config

    # Try to find recent conversations for this contact in our DB
    since = datetime.now(timezone.utc) - timedelta(days=7)

    # Query structured outputs for this contact
    assert store._db is not None
    cursor = await store._db.execute(
        """SELECT outcome, drop_off_stage, sentiment, bot_error_detected,
                  bot_error_notes, notable_quote, summary, timestamp
           FROM structured_outputs
           WHERE contact_id = ?
           ORDER BY timestamp DESC
           LIMIT 5""",
        (contact_id,),
    )
    rows = await cursor.fetchall()

    if not rows:
        # Try Chatrace API if available
        chatrace_client = getattr(request.app.state, "chatrace_client", None)
        if chatrace_client:
            return f"No analyzed conversations found for {contact_id} in our database. Try sending their full phone number including country code."
        return f"No conversations found for {contact_id} in the last 7 days."

    # Format the results
    reply = f"📱 Contact: {contact_id}\n\n"
    for i, row in enumerate(rows, 1):
        outcome, stage, sentiment, errors, error_notes, quote, summary, ts = row
        reply += f"--- Conversation {i} ({ts}) ---\n"
        reply += f"Outcome: {outcome}\n"
        if stage:
            reply += f"Drop-off stage: {stage}\n"
        reply += f"Sentiment: {sentiment}\n"
        if errors:
            reply += f"Bot error: {error_notes or 'Yes'}\n"
        if quote:
            reply += f"Quote: \"{quote}\"\n"
        if summary:
            reply += f"Summary: {summary}\n"
        reply += "\n"

    return reply[:4000]  # Keep under Telegram limit


async def _handle_today(store, config) -> str:
    """Handle /today — summary of today's conversations."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    assert store._db is not None
    cursor = await store._db.execute(
        """SELECT outcome, COUNT(*) as count
           FROM structured_outputs
           WHERE timestamp >= ?
           GROUP BY outcome""",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    rows = await cursor.fetchall()

    if not rows:
        return "📊 No conversations analyzed today yet."

    total = sum(row[1] for row in rows)
    reply = f"📊 Today's Summary ({total} conversations)\n\n"
    for outcome, count in sorted(rows, key=lambda x: -x[1]):
        pct = count / total * 100
        reply += f"• {outcome}: {count} ({pct:.0f}%)\n"

    # Get sentiment breakdown
    cursor = await store._db.execute(
        """SELECT sentiment, COUNT(*) as count
           FROM structured_outputs
           WHERE timestamp >= ?
           GROUP BY sentiment""",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    sentiment_rows = await cursor.fetchall()
    if sentiment_rows:
        reply += "\nSentiment:\n"
        for sentiment, count in sorted(sentiment_rows, key=lambda x: -x[1]):
            reply += f"• {sentiment}: {count}\n"

    # Bot errors today
    cursor = await store._db.execute(
        """SELECT COUNT(*) FROM structured_outputs
           WHERE timestamp >= ? AND bot_error_detected = 1""",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    error_row = await cursor.fetchone()
    if error_row and error_row[0] > 0:
        reply += f"\n⚠️ Bot errors: {error_row[0]}"

    return reply


async def _handle_dropoffs(store, config) -> str:
    """Handle /dropoffs — list contacts that dropped off today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    assert store._db is not None
    cursor = await store._db.execute(
        """SELECT contact_id, drop_off_stage, summary, timestamp
           FROM structured_outputs
           WHERE timestamp >= ? AND outcome = 'dropped_off'
           ORDER BY timestamp DESC
           LIMIT 10""",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    rows = await cursor.fetchall()

    if not rows:
        return "✅ No drop-offs today!"

    reply = f"📉 Drop-offs today ({len(rows)}):\n\n"
    for contact_id, stage, summary, ts in rows:
        reply += f"• {contact_id}"
        if stage:
            reply += f" — dropped at {stage}"
        if summary:
            reply += f"\n  {summary}"
        reply += "\n\n"

    return reply[:4000]


async def _handle_status(store, config) -> str:
    """Handle /status — quick system status."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    assert store._db is not None

    # Conversations today
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM structured_outputs WHERE timestamp >= ?",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    row = await cursor.fetchone()
    convos_today = row[0] if row else 0

    # Conversations this week
    week_start = datetime.now(timezone.utc) - timedelta(days=7)
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM structured_outputs WHERE timestamp >= ?",
        (week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    row = await cursor.fetchone()
    convos_week = row[0] if row else 0

    # Total stored
    cursor = await store._db.execute("SELECT COUNT(*) FROM structured_outputs")
    row = await cursor.fetchone()
    total = row[0] if row else 0

    # Flags today
    cursor = await store._db.execute(
        "SELECT COUNT(*) FROM flag_history WHERE triggered_at >= ?",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    row = await cursor.fetchone()
    flags_today = row[0] if row else 0

    now = datetime.now(timezone.utc)
    reply = (
        f"🖥️ System Status\n\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Conversations today: {convos_today}\n"
        f"Conversations this week: {convos_week}\n"
        f"Total stored: {total}\n"
        f"Alerts triggered today: {flags_today}\n"
        f"Service: running ✅"
    )
    return reply


async def _handle_tag(text: str, request: Request) -> str:
    """Handle /tag <phone> <tag_name> — tag a contact in Chatrace."""
    parts = text.split()
    if len(parts) < 3:
        return "Usage: /tag <phone> <tag_name>\nExample: /tag +601119801333 qualified"

    contact_id = parts[1]
    tag_name = parts[2]

    chatrace_client = getattr(request.app.state, "chatrace_client", None)
    if not chatrace_client:
        return "❌ Chatrace API not configured. Add CHATRACE_API_TOKEN to environment."

    tag_id = await chatrace_client._get_tag_id_by_name(tag_name)
    if not tag_id:
        return f"❌ Tag '{tag_name}' not found in Chatrace. Create it first."

    try:
        import httpx
        url = f"https://api.chatrace.com/contacts/{contact_id}/tags/{tag_id}"
        response = await chatrace_client._http.post(url, headers=chatrace_client._headers)
        if response.status_code in (200, 201):
            return f"✅ Tagged {contact_id} as '{tag_name}'"
        else:
            return f"❌ Failed to tag (status {response.status_code})"
    except Exception as e:
        return f"❌ Error: {str(e)[:200]}"


async def _handle_freeform(text: str, request: Request) -> str:
    """Handle free-form questions — ask NIM about your data."""
    store = request.app.state.store
    analyzer = request.app.state.analyzer

    # Get recent context
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    assert store._db is not None

    cursor = await store._db.execute(
        """SELECT outcome, sentiment, bot_error_detected, summary
           FROM structured_outputs
           WHERE timestamp >= ?
           ORDER BY timestamp DESC
           LIMIT 20""",
        (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    rows = await cursor.fetchall()

    context = "Recent conversations today:\n"
    for row in rows:
        context += f"- outcome: {row[0]}, sentiment: {row[1]}, errors: {'yes' if row[2] else 'no'}, summary: {row[3]}\n"

    if not rows:
        context = "No conversations analyzed today yet."

    # Call NIM with the question + context
    try:
        url = f"{analyzer._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {analyzer._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": analyzer._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an AI assistant helping a business owner monitor their WhatsApp lead-gen chatbot. "
                        "Answer questions about their bot's performance based on the data provided. "
                        "Keep answers concise and actionable. Use bullet points."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Here's my recent data:\n{context}\n\nMy question: {text}",
                },
            ],
            "temperature": 0.3,
            "max_tokens": 500,
        }

        response = await analyzer._http_client.post(
            url, json=payload, headers=headers, timeout=30
        )
        response.raise_for_status()
        data = response.json()

        if "choices" in data and data["choices"]:
            answer = data["choices"][0].get("message", {}).get("content", "")
            return answer[:4000] if answer else "🤔 Couldn't generate an answer."

        return "🤔 NIM didn't return a useful response."

    except Exception as e:
        logger.error("Freeform NIM call failed", extra={"error": str(e)})
        return f"❌ Couldn't process your question: {str(e)[:200]}"
