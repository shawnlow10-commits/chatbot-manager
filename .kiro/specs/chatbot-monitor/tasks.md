# Implementation Plan: Conversation Intelligence Monitor

## Overview

This plan implements a FastAPI service that receives Chatrace webhooks, validates and deduplicates payloads, analyzes conversations via NVIDIA NIM, stores results in SQLite with rolling aggregates, detects anomalies in real-time, and sends Telegram alerts/digests. The implementation follows a bottom-up approach: models and config first, then data layer, core pipeline modules, detection/alerting, scheduling, and finally integration wiring.

## Tasks

- [x] 1. Set up project structure, dependencies, and core models
  - [x] 1.1 Create project directory structure and install dependencies
    - Create `chatbot_monitor/` package with `__init__.py`, `main.py`, `config.py`, `receiver.py`, `validator.py`, `deduplicator.py`, `nim_analyzer.py`, `memory_store.py`, `anomaly_detector.py`, `digest_scheduler.py`, `telegram_notifier.py`, `models.py`
    - Create `chatbot_monitor/prompts/` directory with `analysis.txt`, `analysis_strict.txt`, `digest.txt`
    - Create `tests/` directory with `conftest.py`, `strategies.py`
    - Create `requirements.txt` with: fastapi, uvicorn, aiosqlite, httpx, apscheduler, pydantic, pyyaml, python-dotenv
    - Create `requirements-dev.txt` with: pytest, pytest-asyncio, hypothesis, respx, httpx
    - Create `config.yaml` example with all required keys using placeholder values
    - Create `.env.example` with WEBHOOK_SECRET, NIM_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    - _Requirements: 13.1, 13.2_

  - [x] 1.2 Implement Pydantic data models
    - Create `chatbot_monitor/models.py` with all domain models: `ChatMessage`, `WebhookPayload`, `StructuredOutput`, `RollingAggregates`, `AnomalyAlert`, `DigestSection`, `DigestMessage`, `AlertThresholds`, `ActiveHours`
    - Use Literal types for enum-like fields (outcome, drop_off_stage, sentiment)
    - Add Field constraints (max_length for bot_error_notes=500, notable_quote=300, summary=200)
    - _Requirements: 4.2, 5.1_

- [x] 2. Implement configuration management
  - [x] 2.1 Implement config loader with YAML + environment variable precedence
    - Create `chatbot_monitor/config.py` with `AppConfig` dataclass and `load_config()` function
    - Load from `config.yaml`, then override with environment variables for secrets
    - Validate all required global keys: webhook_secret, nim_api_key, nim_base_url, nim_model, telegram_bot_token, telegram_chat_id, digest_schedule
    - Validate per-client entries have client_id, display_name
    - Raise descriptive errors for missing/malformed config, refuse to start
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6_

  - [ ]* 2.2 Write property tests for configuration (Properties 14, 15)
    - **Property 14: Configuration Environment Variable Precedence** — For any key in both YAML and env var, loaded value SHALL equal the env var value
    - **Validates: Requirements 11.1**
    - **Property 15: Configuration Completeness Validation** — For any config missing required global or per-client fields, load_config SHALL raise an error
    - **Validates: Requirements 11.4, 11.6**

- [x] 3. Implement structured logging
  - [x] 3.1 Implement structured JSON logging with truncation
    - Create logging configuration in `chatbot_monitor/logging_config.py`
    - Output all logs to stdout as structured JSON with ISO 8601 timestamp, level, module, message fields
    - Implement field truncation for values exceeding 10,000 characters
    - Include contact_id in all log entries when processing a specific contact
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6_

  - [ ]* 3.2 Write property test for logging format (Property 16)
    - **Property 16: Structured Log Output Format** — For any log event, output SHALL be valid JSON with timestamp, level, module, message fields; any field > 10,000 chars SHALL be truncated
    - **Validates: Requirements 12.2, 12.5**

- [x] 4. Implement payload validation and deduplication
  - [x] 4.1 Implement payload validator with field checking and truncation
    - Create `chatbot_monitor/validator.py` with validation functions
    - Validate required fields: chat_history (non-empty list), contact_id (string), timestamp (valid ISO 8601)
    - Implement chat_history truncation to last 50 messages when exceeding 50
    - Extract optional fields (tags, last_ref, user_source) when present
    - Return validation errors identifying missing/invalid fields
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [ ]* 4.2 Write property tests for validation (Properties 2, 3, 4)
    - **Property 2: Payload Required Field Validation** — Payload accepted iff it contains chat_history, contact_id, and timestamp
    - **Validates: Requirements 2.1, 2.2**
    - **Property 3: Chat History Truncation Invariant** — For N messages, output has min(N, 50) messages equal to last min(N, 50) entries
    - **Validates: Requirements 2.4**
    - **Property 4: Timestamp Format Validation** — Validator accepts iff timestamp is valid ISO 8601
    - **Validates: Requirements 2.5**

  - [x] 4.3 Implement deduplicator with SHA-256 key computation
    - Create `chatbot_monitor/deduplicator.py` with `compute_dedupe_key(client_id, contact_id, timestamp)` function
    - Normalize timestamp to second precision before hashing
    - Compute SHA-256 of concatenated client_id + contact_id + normalized_timestamp
    - _Requirements: 3.1, 3.5_

  - [ ]* 4.4 Write property test for deduplication (Property 5)
    - **Property 5: Dedupe Key Determinism** — Same inputs always produce same SHA-256; different inputs produce different keys
    - **Validates: Requirements 3.1**

- [x] 5. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Implement Memory Store (SQLite data access layer)
  - [x] 6.1 Implement MemoryStore with schema initialization and CRUD operations
    - Create `chatbot_monitor/memory_store.py` with `MemoryStore` class using aiosqlite
    - Implement `init_db()` with WAL mode, foreign keys, all CREATE TABLE/INDEX statements
    - Implement `has_dedupe_key()`, `store_dedupe_key()`, `store_raw_payload()`
    - Implement `store_structured_output()` with aggregate update in same transaction
    - Implement `get_rolling_aggregates()` using on-demand SQL queries (7d and 30d windows)
    - Implement `get_conversations_since()` and `get_last_conversation_time()`
    - Implement `record_flag()` and `is_in_cooldown()`
    - Implement `purge_old_records()` with configurable retention (30d dedupe, 90d raw/analyses/flags)
    - Implement retry-once on write failures
    - _Requirements: 3.2, 3.3, 3.4, 5.1, 5.2, 5.3, 5.4, 5.5_

  - [ ]* 6.2 Write property tests for Memory Store (Properties 7, 8)
    - **Property 7: Analysis Persistence Round-Trip** — Persisting a StructuredOutput and reading it back SHALL yield identical object
    - **Validates: Requirements 4.2, 5.1**
    - **Property 8: Rolling Aggregate Correctness** — For N records in 7 days, daily volume sum equals count; outcome and sentiment distributions sum to total
    - **Validates: Requirements 5.3**

- [x] 7. Implement NIM Analyzer
  - [x] 7.1 Implement NIM Analyzer with retry and circuit breaker
    - Create `chatbot_monitor/nim_analyzer.py` with `NIMAnalyzer` class
    - Implement `analyze()` method that sends chat_history to NIM with analysis prompt
    - Parse NIM response into `StructuredOutput` model
    - Implement retry with stricter prompt on malformed response (1 retry)
    - Implement exponential backoff (2s→4s→8s) for timeout/HTTP errors (3 retries)
    - Implement circuit breaker: after 5 consecutive failures enter 5-minute cooldown
    - Use configurable model name, base URL, and API key from AppConfig
    - Create prompt templates in `chatbot_monitor/prompts/`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [ ]* 7.2 Write unit tests for NIM Analyzer retry and circuit breaker logic
    - Test malformed response → stricter prompt retry → success path
    - Test timeout → exponential backoff → eventual failure path
    - Test circuit breaker state transitions (closed → open → half-open → closed)
    - _Requirements: 4.4, 4.5, 4.6_

- [x] 8. Implement Telegram Notifier
  - [x] 8.1 Implement Telegram Notifier with message formatting and splitting
    - Create `chatbot_monitor/telegram_notifier.py` with `TelegramNotifier` class
    - Implement `send_alert()`: format with 🚨 prefix, include anomaly type, client name, metric, baseline
    - Implement alert message truncation to 4096 chars preserving prefix, type, and client name
    - Implement `send_digest()`: format with 📊 prefix, one section per client with display_name label
    - Implement digest bullet formatting (max 20 bullets/client, max 280 chars/bullet)
    - Implement message splitting for content > 4096 chars with no data loss
    - Implement exponential backoff retry (2s→4s→8s, 3 retries) on Telegram API errors
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 8.2 Write property tests for Telegram Notifier (Properties 12, 13)
    - **Property 12: Alert Message Formatting** — Message starts with 🚨, contains anomaly_type/client_name/metric/baseline, never exceeds 4096 chars
    - **Validates: Requirements 9.1, 9.2, 9.4**
    - **Property 13: Digest Message Formatting and Splitting** — Starts with 📊, one section per client, ≤20 bullets/client, ≤280 chars/bullet, splits at 4096 with no loss
    - **Validates: Requirements 10.1, 10.2, 10.3, 10.4**

- [x] 9. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. Implement Anomaly Detector
  - [x] 10.1 Implement Anomaly Detector with threshold checks, persistence count, and cooldown
    - Create `chatbot_monitor/anomaly_detector.py` with `AnomalyDetector` class
    - Implement `evaluate()`: check drop-off rate, consecutive errors, low volume, negative sentiment against rolling aggregates
    - Implement persistence count logic: only trigger after N consecutive occurrences meet threshold
    - Implement cooldown check via `is_in_cooldown()` before triggering
    - Implement `record_flag()` on trigger for cooldown tracking
    - Skip evaluation when insufficient data points (< persistence_count)
    - Implement `check_inactive_clients()` for active-hours detection
    - Use per-client thresholds from config with fallback to defaults
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 7.1, 7.2, 7.3, 7.4, 7.5_

  - [ ]* 10.2 Write property tests for Anomaly Detector (Properties 9, 10, 11)
    - **Property 9: Anomaly Detection Correctness** — Alert triggers iff metric exceeds threshold AND persistence count met
    - **Validates: Requirements 6.2, 6.3**
    - **Property 10: Cooldown Suppression** — Alert suppressed if same type/client/stage triggered within cooldown window
    - **Validates: Requirements 6.4, 6.5**
    - **Property 11: Inactive Hours Detection** — Alert if zero conversations during active window; no alert if outside hours or has conversations
    - **Validates: Requirements 7.2, 7.3, 7.4**

- [x] 11. Implement Digest Scheduler
  - [x] 11.1 Implement Digest Scheduler with APScheduler and NIM synthesis
    - Create `chatbot_monitor/digest_scheduler.py` with `DigestScheduler` class
    - Implement `start()` to register APScheduler cron jobs (digest + inactive check + purge)
    - Implement `generate_digest()`: retrieve conversations since last digest, call NIM synthesis, format output
    - Omit clients with zero conversations in digest period
    - Cache synthesis text in digest_log on Telegram failure for retry without re-calling NIM
    - Implement data purge scheduled job (daily at 03:00 UTC)
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [ ]* 11.2 Write unit tests for Digest Scheduler
    - Test digest generation with multi-client data
    - Test client omission when zero conversations
    - Test synthesis caching on Telegram delivery failure
    - Test purge job respects retention boundaries
    - _Requirements: 8.5, 8.6, 8.7_

- [x] 12. Implement Webhook Receiver and wire the pipeline
  - [x] 12.1 Implement webhook receiver endpoint with auth and background processing
    - Create `chatbot_monitor/receiver.py` with POST `/webhook/{client_id}` endpoint
    - Validate `X-Webhook-Secret` header → 401 on mismatch/missing
    - Validate client_id in configured clients → 404 on unknown
    - Check Content-Length ≤ 1MB → 413 on oversized
    - Parse and validate payload via validator module
    - Compute dedupe_key and check for duplicates → skip if exists
    - Store dedupe_key + raw payload, respond 200 immediately
    - Enqueue background task: NIM analysis → persist output → evaluate anomalies → alert
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 3.2, 3.3_

  - [ ]* 12.2 Write property tests for receiver auth and routing (Properties 1, 6)
    - **Property 1: Webhook Authentication and Routing** — 401 if secret wrong, 404 if unknown client, 200 only if both pass
    - **Validates: Requirements 1.2, 1.3, 1.4**
    - **Property 6: Idempotent Processing** — Same payload submitted N times creates exactly one record
    - **Validates: Requirements 3.3**

- [x] 13. Implement FastAPI application factory and lifespan
  - [x] 13.1 Create application entry point with dependency injection and lifespan
    - Create `chatbot_monitor/main.py` with FastAPI app factory
    - Implement lifespan: load config, init MemoryStore, create httpx.AsyncClient, instantiate NIMAnalyzer, AnomalyDetector, TelegramNotifier, DigestScheduler
    - Start DigestScheduler on startup, shut down gracefully on shutdown
    - Wire dependency injection (get_config, get_store, etc.)
    - _Requirements: 13.1, 13.4_

- [x] 14. Checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 15. Write test infrastructure and integration tests
  - [x] 15.1 Create shared test fixtures and Hypothesis strategies
    - Create `tests/conftest.py` with fixtures: in-memory SQLite MemoryStore, mock NIM responses (via respx), mock Telegram API, valid AppConfig
    - Create `tests/strategies.py` with Hypothesis strategies: valid_client_ids, valid_contact_ids, valid_timestamps, chat_messages, chat_histories, structured_outputs, anomaly_alerts, digest_messages, config_dicts
    - _Requirements: 13.4_

  - [ ]* 15.2 Write integration tests for end-to-end pipeline
    - Test full webhook → validation → dedup → analysis → anomaly → alert flow with mocked NIM/Telegram
    - Test duplicate payload rejection (send same webhook twice, verify single record)
    - Test multi-client isolation (one client's anomalies don't affect another)
    - Test digest generation with real DB queries (in-memory SQLite)
    - Test inactive hours check identifies silent clients correctly
    - _Requirements: 1.5, 3.3, 6.1, 7.2, 8.2_

- [x] 16. Create README and documentation
  - [x] 16.1 Create README with deployment and configuration docs
    - Write README.md with sections: deployment steps (Railway/Render), shared secret setup, Telegram bot configuration, NIM API key setup, onboarding new clients, module responsibility descriptions
    - _Requirements: 13.3_

- [x] 17. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation of the implementation
- Property tests validate universal correctness properties defined in the design document
- Unit tests validate specific examples and edge cases
- The implementation language is Python with FastAPI, aiosqlite, httpx, and APScheduler
- All external API calls (NIM, Telegram) should be mocked in tests using respx
- In-memory SQLite (`:memory:`) is used for test isolation

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["2.2", "3.2", "4.1", "4.3"] },
    { "id": 3, "tasks": ["4.2", "4.4", "6.1"] },
    { "id": 4, "tasks": ["6.2", "7.1", "8.1"] },
    { "id": 5, "tasks": ["7.2", "8.2", "10.1"] },
    { "id": 6, "tasks": ["10.2", "11.1"] },
    { "id": 7, "tasks": ["11.2", "12.1"] },
    { "id": 8, "tasks": ["12.2", "13.1"] },
    { "id": 9, "tasks": ["15.1"] },
    { "id": 10, "tasks": ["15.2", "16.1"] }
  ]
}
```
