<<<<<<< HEAD
# Conversation Intelligence Monitor

A FastAPI service that receives webhook payloads from Chatrace (WhatsApp chatbot platform), analyzes completed conversations using NVIDIA NIM, detects anomalies against rolling baselines, and delivers real-time alerts and periodic digests via Telegram.

## Overview

The system operates as a real-time pipeline:

1. **Receive** — Accept webhook POSTs from Chatrace containing completed conversation transcripts
2. **Validate & Deduplicate** — Ensure payload integrity via HMAC secret and prevent reprocessing via SHA-256 deduplication
3. **Analyze** — Extract structured insights (outcome, sentiment, bot errors) via NVIDIA NIM chat completions API
4. **Persist** — Store raw payloads, structured outputs, and rolling aggregates in SQLite
5. **Detect** — Compare per-conversation metrics against 7-day/30-day rolling baselines to identify anomalies
6. **Alert** — Deliver real-time Telegram alerts (🚨) for anomalies and periodic digest summaries (📊)

Supports multiple clients/bots, each with independent thresholds and active-hours definitions.

## Architecture

```
Chatrace → POST /webhook/{client_id}
    → Receiver (auth + size check)
    → Validator (required fields, truncation)
    → Deduplicator (SHA-256 key check)
    → [Background Task]
        → NIM Analyzer (structured JSON extraction)
        → Memory Store (SQLite persistence + aggregate update)
        → Anomaly Detector (threshold + persistence + cooldown)
        → Telegram Notifier (alert delivery)

APScheduler (cron)
    → Digest Scheduler → NIM synthesis → Telegram digest
    → Inactive Hours Check → Telegram alert
    → Data Purge (90-day retention)
```

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token (from @BotFather)
- An NVIDIA NIM API key (from build.nvidia.com)

### Local Development

```bash
# Clone and enter the project
cd "chatbot manager"

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements-dev.txt

# Copy and configure environment
copy .env.example .env
# Edit .env with your actual secrets

# Edit config.yaml with your client definitions

# Run the service
uvicorn chatbot_monitor.main:app --reload --port 8000
```

The webhook endpoint will be available at `http://localhost:8000/webhook/{client_id}`.

## Deployment

### Railway

1. Push the project to a GitHub repository
2. Create a new project on Railway and connect the repo
3. Set the start command:
   ```
   uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port $PORT
   ```
4. Add environment variables in the Railway dashboard (see Configuration section)
5. Railway will detect `requirements.txt` and install dependencies automatically
6. Deploy — the service will be available at the generated Railway URL

### Render

1. Push the project to a GitHub repository
2. Create a new **Web Service** on Render and connect the repo
3. Set:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables in the Render dashboard (see Configuration section)
5. Deploy — the service will be available at the generated Render URL

### Post-Deployment

- Note your service URL (e.g., `https://your-app.up.railway.app`)
- Configure Chatrace External Request to point at `https://your-app.up.railway.app/webhook/{client_id}`
- Verify the webhook is working by triggering a test conversation and checking Telegram

## Configuration

The system loads configuration from two sources:

1. **`config.yaml`** — Structure, client definitions, thresholds, and non-secret settings
2. **Environment variables (`.env`)** — Secrets that override YAML placeholders

Environment variables always take precedence over `config.yaml` values.

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `WEBHOOK_SECRET` | Shared secret for authenticating incoming Chatrace webhooks |
| `NIM_API_KEY` | NVIDIA NIM API key for conversation analysis |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID where alerts and digests are sent |

### config.yaml Structure

```yaml
webhook_secret: "${WEBHOOK_SECRET}"      # Overridden by env var

nim:
  api_key: "${NIM_API_KEY}"              # Overridden by env var
  base_url: "https://integrate.api.nvidia.com/v1"
  model: "meta/llama-3.1-70b-instruct"
  timeout_seconds: 30

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"     # Overridden by env var
  chat_id: "${TELEGRAM_CHAT_ID}"         # Overridden by env var

digest:
  schedule: "0 8 * * *"                  # Cron expression (daily 08:00 UTC)

alert_defaults:
  dropoff_rate_pct: 50                   # Alert if drop-off exceeds baseline by 50%
  consecutive_errors: 3                  # Alert after 3 consecutive bot errors
  low_volume_pct: 50                     # Alert if volume drops 50% below baseline
  consecutive_negative_sentiment: 3      # Alert after 3 consecutive negative/frustrated
  persistence_count: 3                   # Required consecutive occurrences before alerting
  cooldown_minutes: 60                   # Suppress repeat alerts for 60 minutes

inactive_check_interval_minutes: 60      # Check for silent clients every 60 min
db_path: "data/monitor.db"              # SQLite database path

clients:
  - client_id: "bot_realestate"
    display_name: "Real Estate Bot"
    active_hours:
      start_time: "08:00"
      end_time: "22:00"
      timezone: "America/Sao_Paulo"
      days: [0, 1, 2, 3, 4]             # Monday–Friday
    thresholds:
      dropoff_rate_pct: 40               # Override default for this client
      cooldown_minutes: 90
```

Per-client `thresholds` are optional — any omitted value falls back to `alert_defaults`.

## Shared Secret Setup

The webhook secret authenticates incoming requests from Chatrace. Both the monitor and Chatrace must share the same secret value.

### 1. Generate a Secret

```bash
# Generate a random secret (use any method you prefer)
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Configure the Monitor

Add the generated secret to your `.env` file:

```
WEBHOOK_SECRET=your-generated-secret-here
```

### 3. Configure Chatrace

In Chatrace, go to the flow where conversations complete and configure the **External Request** block:

- **URL:** `https://your-deployed-url/webhook/{client_id}`
- **Method:** POST
- **Headers:** Add `X-Webhook-Secret` with the same secret value

The monitor will reject any request where the `X-Webhook-Secret` header doesn't match.

## Telegram Bot Setup

### 1. Create a Bot via BotFather

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts to choose a name and username
4. Copy the **HTTP API token** — this is your `TELEGRAM_BOT_TOKEN`

### 2. Get Your Chat ID

**For personal chat:**
1. Send any message to your new bot
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
3. Find the `"chat": {"id": ...}` value — this is your `TELEGRAM_CHAT_ID`

**For a group:**
1. Add the bot to the group
2. Send a message in the group
3. Call `getUpdates` as above — the group chat ID will be negative (e.g., `-1001234567890`)

### 3. Configure

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=-1001234567890
```

## NIM API Key Setup

1. Go to [build.nvidia.com](https://build.nvidia.com)
2. Sign in or create an NVIDIA account
3. Browse available models and select one (default: `meta/llama-3.1-70b-instruct`)
4. Click **Get API Key** and generate a new key
5. Add it to your `.env`:

```
NIM_API_KEY=nvapi-your-key-here
```

The model name in `config.yaml` must match a model available through NIM. You can change it under `nim.model`.

## Onboarding a New Client

To add a new Chatrace bot/client to the monitor:

### 1. Add to config.yaml

```yaml
clients:
  # ... existing clients ...
  - client_id: "bot_newclient"
    display_name: "New Client Bot"
    active_hours:
      start_time: "09:00"
      end_time: "18:00"
      timezone: "America/Sao_Paulo"
      days: [0, 1, 2, 3, 4]
    thresholds: {}  # Uses alert_defaults
```

- `client_id` must be unique and URL-safe (it appears in the webhook path)
- `display_name` is what appears in Telegram alerts and digests
- `active_hours` is optional — without it, no inactive-hours alerts are generated
- `thresholds` can override specific defaults or be empty to use global defaults

### 2. Configure Chatrace

In the new client's Chatrace flow, add an **External Request** block at the end of the conversation:

- **URL:** `https://your-deployed-url/webhook/bot_newclient`
- **Method:** POST
- **Headers:** `X-Webhook-Secret: <your-shared-secret>`
- **Body:** Include `contact_id`, `timestamp` (ISO 8601), and `chat_history` (array of messages)

### 3. Restart the Service

The service reads `config.yaml` on startup, so restart after adding a new client.

## Module Descriptions

| Module | File | Responsibility |
|--------|------|----------------|
| **Receiver** | `receiver.py` | HTTP endpoint, header authentication, size limit enforcement, background task dispatch |
| **Validator** | `validator.py` | Payload schema validation, required field checks, chat history truncation (50 msg limit) |
| **Deduplicator** | `deduplicator.py` | SHA-256 dedupe key computation with timestamp normalization |
| **NIM Analyzer** | `nim_analyzer.py` | NVIDIA NIM API client, prompt management, structured JSON extraction with retry logic |
| **Memory Store** | `memory_store.py` | SQLite data access layer — raw payloads, structured outputs, rolling aggregates, flag history |
| **Anomaly Detector** | `anomaly_detector.py` | Threshold comparison against rolling baselines, persistence counting, cooldown enforcement |
| **Digest Scheduler** | `digest_scheduler.py` | APScheduler cron jobs — periodic digest generation, inactive-hours checks, data purge |
| **Telegram Notifier** | `telegram_notifier.py` | Telegram Bot API client — alert/digest formatting, message splitting, retry with backoff |
| **Config** | `config.py` | YAML + env var loading, validation, AppConfig construction |
| **Models** | `models.py` | Pydantic models for payloads, structured outputs, alerts, digests, and configuration |
| **Logging** | `logging_config.py` | Structured JSON logging to stdout with field truncation |
| **Main** | `main.py` | FastAPI app factory, lifespan management, dependency injection |

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_receiver.py

# Run property-based tests with more examples
pytest --hypothesis-seed=0 -v
```

Tests use:
- **pytest** + **pytest-asyncio** for async test support
- **hypothesis** for property-based testing
- **respx** for mocking HTTP calls to NIM and Telegram APIs
- In-memory SQLite (`:memory:`) for test isolation

## API Reference

### POST /webhook/{client_id}

Receives a completed conversation payload from Chatrace.

**Headers:**
- `X-Webhook-Secret` (required) — Must match the configured shared secret
- `Content-Type: application/json`

**Path Parameters:**
- `client_id` — Must match a configured client in `config.yaml`

**Request Body:**
```json
{
  "contact_id": "5511999998888",
  "timestamp": "2024-10-15T14:32:00Z",
  "chat_history": [
    {"role": "bot", "content": "Hello! How can I help you?"},
    {"role": "user", "content": "I'm interested in the apartment listing"}
  ],
  "tags": ["organic"],
  "last_ref": "campaign_oct",
  "user_source": "instagram"
}
```

**Required fields:** `contact_id`, `timestamp` (ISO 8601), `chat_history` (non-empty array)
**Optional fields:** `tags`, `last_ref`, `user_source`

**Responses:**

| Status | Condition |
|--------|-----------|
| 200 | Payload accepted (or duplicate, or validation error — logged internally) |
| 401 | Missing or invalid `X-Webhook-Secret` header |
| 404 | Unknown `client_id` |
| 413 | Request body exceeds 1MB |
"# chatbot-manager" 
"# chatbot-manager" 
=======
# Conversation Intelligence Monitor

A FastAPI service that receives webhook payloads from Chatrace (WhatsApp chatbot platform), analyzes completed conversations using NVIDIA NIM, detects anomalies against rolling baselines, and delivers real-time alerts and periodic digests via Telegram.

## Overview

The system operates as a real-time pipeline:

1. **Receive** — Accept webhook POSTs from Chatrace containing completed conversation transcripts
2. **Validate & Deduplicate** — Ensure payload integrity via HMAC secret and prevent reprocessing via SHA-256 deduplication
3. **Analyze** — Extract structured insights (outcome, sentiment, bot errors) via NVIDIA NIM chat completions API
4. **Persist** — Store raw payloads, structured outputs, and rolling aggregates in SQLite
5. **Detect** — Compare per-conversation metrics against 7-day/30-day rolling baselines to identify anomalies
6. **Alert** — Deliver real-time Telegram alerts (🚨) for anomalies and periodic digest summaries (📊)

Supports multiple clients/bots, each with independent thresholds and active-hours definitions.

## Architecture

```
Chatrace → POST /webhook/{client_id}
    → Receiver (auth + size check)
    → Validator (required fields, truncation)
    → Deduplicator (SHA-256 key check)
    → [Background Task]
        → NIM Analyzer (structured JSON extraction)
        → Memory Store (SQLite persistence + aggregate update)
        → Anomaly Detector (threshold + persistence + cooldown)
        → Telegram Notifier (alert delivery)

APScheduler (cron)
    → Digest Scheduler → NIM synthesis → Telegram digest
    → Inactive Hours Check → Telegram alert
    → Data Purge (90-day retention)
```

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token (from @BotFather)
- An NVIDIA NIM API key (from build.nvidia.com)

### Local Development

```bash
# Clone and enter the project
cd "chatbot manager"

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements-dev.txt

# Copy and configure environment
copy .env.example .env
# Edit .env with your actual secrets

# Edit config.yaml with your client definitions

# Run the service
uvicorn chatbot_monitor.main:app --reload --port 8000
```

The webhook endpoint will be available at `http://localhost:8000/webhook/{client_id}`.

## Deployment

### Railway

1. Push the project to a GitHub repository
2. Create a new project on Railway and connect the repo
3. Set the start command:
   ```
   uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port $PORT
   ```
4. Add environment variables in the Railway dashboard (see Configuration section)
5. Railway will detect `requirements.txt` and install dependencies automatically
6. Deploy — the service will be available at the generated Railway URL

### Render

1. Push the project to a GitHub repository
2. Create a new **Web Service** on Render and connect the repo
3. Set:
   - **Runtime:** Python
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn chatbot_monitor.main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables in the Render dashboard (see Configuration section)
5. Deploy — the service will be available at the generated Render URL

### Post-Deployment

- Note your service URL (e.g., `https://your-app.up.railway.app`)
- Configure Chatrace External Request to point at `https://your-app.up.railway.app/webhook/{client_id}`
- Verify the webhook is working by triggering a test conversation and checking Telegram

## Configuration

The system loads configuration from two sources:

1. **`config.yaml`** — Structure, client definitions, thresholds, and non-secret settings
2. **Environment variables (`.env`)** — Secrets that override YAML placeholders

Environment variables always take precedence over `config.yaml` values.

### Required Environment Variables

| Variable | Description |
|----------|-------------|
| `WEBHOOK_SECRET` | Shared secret for authenticating incoming Chatrace webhooks |
| `NIM_API_KEY` | NVIDIA NIM API key for conversation analysis |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API token from @BotFather |
| `TELEGRAM_CHAT_ID` | Telegram chat/group ID where alerts and digests are sent |

### config.yaml Structure

```yaml
webhook_secret: "${WEBHOOK_SECRET}"      # Overridden by env var

nim:
  api_key: "${NIM_API_KEY}"              # Overridden by env var
  base_url: "https://integrate.api.nvidia.com/v1"
  model: "meta/llama-3.1-70b-instruct"
  timeout_seconds: 30

telegram:
  bot_token: "${TELEGRAM_BOT_TOKEN}"     # Overridden by env var
  chat_id: "${TELEGRAM_CHAT_ID}"         # Overridden by env var

digest:
  schedule: "0 8 * * *"                  # Cron expression (daily 08:00 UTC)

alert_defaults:
  dropoff_rate_pct: 50                   # Alert if drop-off exceeds baseline by 50%
  consecutive_errors: 3                  # Alert after 3 consecutive bot errors
  low_volume_pct: 50                     # Alert if volume drops 50% below baseline
  consecutive_negative_sentiment: 3      # Alert after 3 consecutive negative/frustrated
  persistence_count: 3                   # Required consecutive occurrences before alerting
  cooldown_minutes: 60                   # Suppress repeat alerts for 60 minutes

inactive_check_interval_minutes: 60      # Check for silent clients every 60 min
db_path: "data/monitor.db"              # SQLite database path

clients:
  - client_id: "bot_realestate"
    display_name: "Real Estate Bot"
    active_hours:
      start_time: "08:00"
      end_time: "22:00"
      timezone: "America/Sao_Paulo"
      days: [0, 1, 2, 3, 4]             # Monday–Friday
    thresholds:
      dropoff_rate_pct: 40               # Override default for this client
      cooldown_minutes: 90
```

Per-client `thresholds` are optional — any omitted value falls back to `alert_defaults`.

## Shared Secret Setup

The webhook secret authenticates incoming requests from Chatrace. Both the monitor and Chatrace must share the same secret value.

### 1. Generate a Secret

```bash
# Generate a random secret (use any method you prefer)
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 2. Configure the Monitor

Add the generated secret to your `.env` file:

```
WEBHOOK_SECRET=your-generated-secret-here
```

### 3. Configure Chatrace

In Chatrace, go to the flow where conversations complete and configure the **External Request** block:

- **URL:** `https://your-deployed-url/webhook/{client_id}`
- **Method:** POST
- **Headers:** Add `X-Webhook-Secret` with the same secret value

The monitor will reject any request where the `X-Webhook-Secret` header doesn't match.

## Telegram Bot Setup

### 1. Create a Bot via BotFather

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts to choose a name and username
4. Copy the **HTTP API token** — this is your `TELEGRAM_BOT_TOKEN`

### 2. Get Your Chat ID

**For personal chat:**
1. Send any message to your new bot
2. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser
3. Find the `"chat": {"id": ...}` value — this is your `TELEGRAM_CHAT_ID`

**For a group:**
1. Add the bot to the group
2. Send a message in the group
3. Call `getUpdates` as above — the group chat ID will be negative (e.g., `-1001234567890`)

### 3. Configure

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
TELEGRAM_CHAT_ID=-1001234567890
```

## NIM API Key Setup

1. Go to [build.nvidia.com](https://build.nvidia.com)
2. Sign in or create an NVIDIA account
3. Browse available models and select one (default: `meta/llama-3.1-70b-instruct`)
4. Click **Get API Key** and generate a new key
5. Add it to your `.env`:

```
NIM_API_KEY=nvapi-your-key-here
```

The model name in `config.yaml` must match a model available through NIM. You can change it under `nim.model`.

## Onboarding a New Client

To add a new Chatrace bot/client to the monitor:

### 1. Add to config.yaml

```yaml
clients:
  # ... existing clients ...
  - client_id: "bot_newclient"
    display_name: "New Client Bot"
    active_hours:
      start_time: "09:00"
      end_time: "18:00"
      timezone: "America/Sao_Paulo"
      days: [0, 1, 2, 3, 4]
    thresholds: {}  # Uses alert_defaults
```

- `client_id` must be unique and URL-safe (it appears in the webhook path)
- `display_name` is what appears in Telegram alerts and digests
- `active_hours` is optional — without it, no inactive-hours alerts are generated
- `thresholds` can override specific defaults or be empty to use global defaults

### 2. Configure Chatrace

In the new client's Chatrace flow, add an **External Request** block at the end of the conversation:

- **URL:** `https://your-deployed-url/webhook/bot_newclient`
- **Method:** POST
- **Headers:** `X-Webhook-Secret: <your-shared-secret>`
- **Body:** Include `contact_id`, `timestamp` (ISO 8601), and `chat_history` (array of messages)

### 3. Restart the Service

The service reads `config.yaml` on startup, so restart after adding a new client.

## Module Descriptions

| Module | File | Responsibility |
|--------|------|----------------|
| **Receiver** | `receiver.py` | HTTP endpoint, header authentication, size limit enforcement, background task dispatch |
| **Validator** | `validator.py` | Payload schema validation, required field checks, chat history truncation (50 msg limit) |
| **Deduplicator** | `deduplicator.py` | SHA-256 dedupe key computation with timestamp normalization |
| **NIM Analyzer** | `nim_analyzer.py` | NVIDIA NIM API client, prompt management, structured JSON extraction with retry logic |
| **Memory Store** | `memory_store.py` | SQLite data access layer — raw payloads, structured outputs, rolling aggregates, flag history |
| **Anomaly Detector** | `anomaly_detector.py` | Threshold comparison against rolling baselines, persistence counting, cooldown enforcement |
| **Digest Scheduler** | `digest_scheduler.py` | APScheduler cron jobs — periodic digest generation, inactive-hours checks, data purge |
| **Telegram Notifier** | `telegram_notifier.py` | Telegram Bot API client — alert/digest formatting, message splitting, retry with backoff |
| **Config** | `config.py` | YAML + env var loading, validation, AppConfig construction |
| **Models** | `models.py` | Pydantic models for payloads, structured outputs, alerts, digests, and configuration |
| **Logging** | `logging_config.py` | Structured JSON logging to stdout with field truncation |
| **Main** | `main.py` | FastAPI app factory, lifespan management, dependency injection |

## Testing

```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/test_receiver.py

# Run property-based tests with more examples
pytest --hypothesis-seed=0 -v
```

Tests use:
- **pytest** + **pytest-asyncio** for async test support
- **hypothesis** for property-based testing
- **respx** for mocking HTTP calls to NIM and Telegram APIs
- In-memory SQLite (`:memory:`) for test isolation

## API Reference

### POST /webhook/{client_id}

Receives a completed conversation payload from Chatrace.

**Headers:**
- `X-Webhook-Secret` (required) — Must match the configured shared secret
- `Content-Type: application/json`

**Path Parameters:**
- `client_id` — Must match a configured client in `config.yaml`

**Request Body:**
```json
{
  "contact_id": "5511999998888",
  "timestamp": "2024-10-15T14:32:00Z",
  "chat_history": [
    {"role": "bot", "content": "Hello! How can I help you?"},
    {"role": "user", "content": "I'm interested in the apartment listing"}
  ],
  "tags": ["organic"],
  "last_ref": "campaign_oct",
  "user_source": "instagram"
}
```

**Required fields:** `contact_id`, `timestamp` (ISO 8601), `chat_history` (non-empty array)
**Optional fields:** `tags`, `last_ref`, `user_source`

**Responses:**

| Status | Condition |
|--------|-----------|
| 200 | Payload accepted (or duplicate, or validation error — logged internally) |
| 401 | Missing or invalid `X-Webhook-Secret` header |
| 404 | Unknown `client_id` |
| 413 | Request body exceeds 1MB |
"# chatbot-manager" 
"# chatbot-manager" 
>>>>>>> a95271f (Pin Python 3.11 for Render compatibility)
