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
        elif text.startswith("/retag"):
            reply = await _handle_retag(request)
        elif text.startswith("/sync"):
            reply = await _handle_sync(request)
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
        "/sync — Re-sync contacts from Chatrace\n"
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

    # Use get_conversations_since with a wide window to find this contact's data
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=30)
    all_convos = await store.get_conversations_since(
        request.app.state.config.clients.get("gjbc", list(request.app.state.config.clients.keys())[0]) if request.app.state.config.clients else "gjbc",
        since,
    )

    # Try Supabase direct query if available
    try:
        from chatbot_monitor.supabase_store import SupabaseStore
        if isinstance(store, SupabaseStore):
            url = f"{store._url}/rest/v1/structured_outputs?contact_id=eq.{contact_id}&select=outcome,drop_off_stage,sentiment,bot_error_detected,bot_error_notes,notable_quote,summary,timestamp&order=timestamp.desc&limit=5"
            resp = await store._http.get(url, headers=store._headers)
            if resp.status_code == 200:
                rows = resp.json()
                if rows:
                    reply = f"📱 Contact: {contact_id}\n\n"
                    for i, row in enumerate(rows, 1):
                        reply += f"--- Conversation {i} ({row.get('timestamp', 'N/A')}) ---\n"
                        reply += f"Outcome: {row.get('outcome')}\n"
                        if row.get('drop_off_stage'):
                            reply += f"Drop-off stage: {row['drop_off_stage']}\n"
                        reply += f"Sentiment: {row.get('sentiment')}\n"
                        if row.get('bot_error_detected'):
                            reply += f"Bot error: {row.get('bot_error_notes') or 'Yes'}\n"
                        if row.get('notable_quote'):
                            reply += f"Quote: \"{row['notable_quote']}\"\n"
                        if row.get('summary'):
                            reply += f"Summary: {row['summary']}\n"
                        reply += "\n"
                    return reply[:4000]
                else:
                    return f"No conversations found for {contact_id}."
    except Exception:
        pass

    # Fallback: try raw SQL for SQLite
    try:
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
            return f"No conversations found for {contact_id}."

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
        return reply[:4000]
    except Exception as e:
        return f"Error looking up contact: {str(e)[:200]}"


async def _handle_today(store, config) -> str:
    """Handle /today — summary of today's conversations."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Use the store's get_conversations_since method (works with both SQLite and Supabase)
    # Get conversations for all clients
    all_convos = []
    for client_id in config.clients:
        convos = await store.get_conversations_since(client_id, today_start)
        all_convos.extend(convos)

    if not all_convos:
        return "📊 No conversations analyzed today yet."

    total = len(all_convos)
    outcomes = {}
    sentiments = {}
    errors = 0

    for c in all_convos:
        o = c.outcome.value if hasattr(c.outcome, "value") else str(c.outcome)
        s = c.sentiment.value if hasattr(c.sentiment, "value") else str(c.sentiment)
        outcomes[o] = outcomes.get(o, 0) + 1
        sentiments[s] = sentiments.get(s, 0) + 1
        if c.bot_error_detected:
            errors += 1

    reply = f"📊 Today's Summary ({total} conversations)\n\n"
    for outcome, count in sorted(outcomes.items(), key=lambda x: -x[1]):
        pct = count / total * 100
        reply += f"• {outcome}: {count} ({pct:.0f}%)\n"

    reply += "\nSentiment:\n"
    for sentiment, count in sorted(sentiments.items(), key=lambda x: -x[1]):
        reply += f"• {sentiment}: {count}\n"

    if errors > 0:
        reply += f"\n⚠️ Bot errors: {errors}"

    return reply


async def _handle_dropoffs(store, config) -> str:
    """Handle /dropoffs — list contacts that dropped off today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # Try Supabase direct query
    try:
        from chatbot_monitor.supabase_store import SupabaseStore
        if isinstance(store, SupabaseStore):
            ts = today_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{store._url}/rest/v1/structured_outputs?timestamp=gte.{ts}&outcome=eq.dropped_off&select=contact_id,drop_off_stage,summary,timestamp&order=timestamp.desc&limit=10"
            resp = await store._http.get(url, headers=store._headers)
            if resp.status_code == 200:
                rows = resp.json()
                if not rows:
                    return "✅ No drop-offs today!"
                reply = f"📉 Drop-offs today ({len(rows)}):\n\n"
                for row in rows:
                    reply += f"• {row.get('contact_id', 'Unknown')}"
                    if row.get('drop_off_stage'):
                        reply += f" — dropped at {row['drop_off_stage']}"
                    if row.get('summary'):
                        reply += f"\n  {row['summary']}"
                    reply += "\n\n"
                return reply[:4000]
    except Exception:
        pass

    # Fallback: get all conversations and filter
    all_dropoffs = []
    for client_id in config.clients:
        convos = await store.get_conversations_since(client_id, today_start)
        for c in convos:
            outcome = c.outcome.value if hasattr(c.outcome, "value") else str(c.outcome)
            if outcome == "dropped_off":
                all_dropoffs.append(c)

    if not all_dropoffs:
        return "✅ No drop-offs today!"

    reply = f"📉 Drop-offs today ({len(all_dropoffs)}):\n\n"
    for c in all_dropoffs[:10]:
        stage = c.drop_off_stage.value if c.drop_off_stage else None
        reply += f"• Dropped"
        if stage:
            reply += f" at {stage}"
        if c.summary:
            reply += f"\n  {c.summary}"
        reply += "\n\n"
    return reply[:4000]


async def _handle_status(store, config) -> str:
    """Handle /status — quick system status."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = datetime.now(timezone.utc) - timedelta(days=7)

    convos_today = 0
    convos_week = 0
    for client_id in config.clients:
        today_convos = await store.get_conversations_since(client_id, today_start)
        week_convos = await store.get_conversations_since(client_id, week_start)
        convos_today += len(today_convos)
        convos_week += len(week_convos)

    now = datetime.now(timezone.utc)
    reply = (
        f"🖥️ System Status\n\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Conversations today: {convos_today}\n"
        f"Conversations this week: {convos_week}\n"
        f"Storage: {'Supabase (persistent)' if hasattr(store, '_url') else 'SQLite (ephemeral)'}\n"
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


async def _handle_sync(request: Request) -> str:
    """Handle /sync — manually trigger a bulk re-sync from Chatrace."""
    chatrace_client = getattr(request.app.state, "chatrace_client", None)
    if not chatrace_client:
        return "❌ Chatrace API not configured. Add CHATRACE_API_TOKEN to environment."

    store = request.app.state.store
    analyzer = request.app.state.analyzer
    config = request.app.state.config

    total_synced = 0
    for cid in config.clients:
        try:
            synced = await chatrace_client.bulk_sync_contacts(store, analyzer, cid)
            total_synced += synced
        except Exception as e:
            logger.error(f"Sync failed for {cid}: {e}")

    if total_synced > 0:
        return f"✅ Synced {total_synced} contacts from Chatrace"
    else:
        return "ℹ️ No new contacts to sync (all already in DB or no chat history found)"


async def _handle_retag(request: Request) -> str:
    """Handle /retag — re-apply tags to all contacts in the database based on their analysis."""
    chatrace_client = getattr(request.app.state, "chatrace_client", None)
    if not chatrace_client:
        return "❌ Chatrace API not configured. Add CHATRACE_API_TOKEN to environment."

    store = request.app.state.store
    config = request.app.state.config

    # Get all analyzed conversations from Supabase
    try:
        from chatbot_monitor.supabase_store import SupabaseStore
        from chatbot_monitor.models import StructuredOutput

        if isinstance(store, SupabaseStore):
            url = f"{store._url}/rest/v1/structured_outputs?select=contact_id,outcome,drop_off_stage,sentiment,bot_error_detected,bot_error_notes,notable_quote,summary&order=timestamp.desc"
            resp = await store._http.get(url, headers=store._headers)
            if resp.status_code != 200:
                return f"❌ Failed to fetch conversations from Supabase (status {resp.status_code})"

            rows = resp.json()
            if not rows:
                return "ℹ️ No analyzed conversations in the database to retag."

            tagged = 0
            failed = 0
            for row in rows:
                contact_id = row.get("contact_id", "")
                if not contact_id:
                    continue

                # Strip the + from phone to get Chatrace numeric ID
                chatrace_id = contact_id.lstrip("+") if contact_id.startswith("+") else contact_id

                try:
                    analysis = StructuredOutput(
                        outcome=row["outcome"],
                        drop_off_stage=row.get("drop_off_stage"),
                        sentiment=row["sentiment"],
                        bot_error_detected=bool(row.get("bot_error_detected")),
                        bot_error_notes=row.get("bot_error_notes"),
                        notable_quote=row.get("notable_quote"),
                        summary=row.get("summary", ""),
                    )
                    await chatrace_client.sync_analysis_to_contact(
                        contact_id=chatrace_id,
                        analysis=analysis,
                        client_id="gjbc",
                    )
                    tagged += 1
                except Exception as e:
                    failed += 1
                    logger.warning(f"Failed to retag {contact_id}: {e}")

            return f"✅ Retagged {tagged} contacts ({failed} failed)"
        else:
            return "❌ Retag only works with Supabase storage."
    except Exception as e:
        return f"❌ Error: {str(e)[:200]}"
