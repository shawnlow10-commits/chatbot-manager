# Requirements Document

## Introduction

The Conversation Intelligence Monitor is a system that receives webhook payloads from Chatrace (a WhatsApp lead-generation chatbot platform), analyzes completed conversations using NVIDIA NIM, maintains rolling baselines in SQLite, detects anomalies in real-time, and delivers alerts and periodic digests via Telegram. It serves multiple clients/bots and operates as a continuously running FastAPI service.

## Glossary

- **Receiver**: The FastAPI HTTP endpoint that accepts incoming webhook POST requests from Chatrace
- **NIM_Analyzer**: The module that calls the NVIDIA NIM chat completions API to extract structured analysis from conversation transcripts
- **Memory_Store**: The SQLite-backed persistence layer that stores raw payloads, analyzed results, rolling aggregates, and flag history
- **Anomaly_Detector**: The module that compares per-conversation analysis results against rolling baselines to identify anomalies
- **Digest_Scheduler**: The APScheduler-based scheduled job that synthesizes periodic summaries using NIM and delivers them via Telegram
- **Telegram_Notifier**: The module that sends immediate alert flags and scheduled digest messages via the Telegram Bot API
- **Client**: A specific bot/business identified by a unique client_id, each with its own configuration and thresholds
- **Conversation**: A completed WhatsApp chat session delivered by Chatrace as a webhook payload containing chat_history and metadata
- **Dedupe_Key**: A unique identifier computed as a hash of client_id + contact_id + timestamp, used to prevent duplicate processing
- **Rolling_Aggregate**: A computed statistical summary (lead volume, outcome mix, drop-off rate, sentiment mix) over trailing 7-day and 30-day windows per client
- **Flag_Cooldown**: A configurable time window during which the same anomaly type for the same client and stage will not trigger a repeated alert
- **Structured_Output**: The JSON object returned by NIM_Analyzer containing outcome, drop_off_stage, sentiment, bot_error_detected, bot_error_notes, notable_quote, and summary fields

## Requirements

### Requirement 1: Webhook Reception

**User Story:** As a system operator, I want the system to receive webhook payloads from Chatrace so that completed conversations are captured for analysis.

#### Acceptance Criteria

1. THE Receiver SHALL expose a POST endpoint at the path /webhook/{client_id}
2. WHEN a POST request is received at /webhook/{client_id}, THE Receiver SHALL validate that the X-Webhook-Secret header matches the configured shared secret
3. IF a request arrives without a valid X-Webhook-Secret header or with a missing header, THEN THE Receiver SHALL respond with HTTP 401 and discard the request body without logging the payload contents
4. IF a request arrives with a client_id that is not present in the configured clients list, THEN THE Receiver SHALL respond with HTTP 404 and log a warning including the unrecognized client_id
5. WHEN a valid webhook payload is received, THE Receiver SHALL respond with HTTP 200 within 500ms before initiating analysis
6. WHEN a valid webhook payload is received, THE Receiver SHALL store the raw payload in the Memory_Store with the associated client_id and a generated dedupe_key
7. IF a request body exceeds 1MB in size, THEN THE Receiver SHALL respond with HTTP 413 and discard the request without further processing

### Requirement 2: Payload Validation

**User Story:** As a system operator, I want incoming payloads validated so that malformed data does not corrupt the analysis pipeline.

#### Acceptance Criteria

1. WHEN a webhook payload is received, THE Receiver SHALL verify that the payload contains chat_history, contact_id, and timestamp fields
2. IF the payload is missing any required field (chat_history, contact_id, or timestamp), THEN THE Receiver SHALL respond with HTTP 200, log a validation error identifying the missing fields, and skip further processing
3. IF the chat_history field is empty or contains no messages, THEN THE Receiver SHALL log a warning and skip analysis for that conversation
4. THE Receiver SHALL accept chat_history containing up to 50 messages and truncate any history exceeding 50 messages to the most recent 50
5. THE Receiver SHALL validate that the timestamp field is in ISO 8601 format; IF the timestamp is not valid ISO 8601, THEN THE Receiver SHALL log a validation error and skip further processing
6. WHEN a valid payload is received, THE Receiver SHALL extract client_id from the URL path and contact_id, timestamp, chat_history, and optional fields (tags, last_ref, user_source) from the request body

### Requirement 3: Idempotent Processing

**User Story:** As a system operator, I want duplicate webhook deliveries to be handled gracefully so that conversations are not analyzed more than once.

#### Acceptance Criteria

1. WHEN a webhook payload is received, THE Receiver SHALL compute a dedupe_key as the SHA-256 hash of the concatenation of client_id + contact_id + timestamp (with timestamp normalized to second precision)
2. WHEN a webhook payload is received with a dedupe_key not present in the Memory_Store, THE Receiver SHALL store the dedupe_key in the Memory_Store before initiating analysis
3. IF a payload arrives with a dedupe_key that already exists in the Memory_Store, THEN THE Receiver SHALL respond with HTTP 200 and skip reprocessing
4. THE Memory_Store SHALL retain dedupe_keys for a minimum of 30 days to support deduplication
5. IF the dedupe_key computation fails due to missing contact_id or timestamp, THEN THE Receiver SHALL log the error and reject the payload as invalid per Requirement 2

### Requirement 4: Per-Conversation Analysis

**User Story:** As a system operator, I want each conversation analyzed by NIM so that I receive structured insights about outcomes and bot performance.

#### Acceptance Criteria

1. WHEN a new conversation is stored, THE NIM_Analyzer SHALL send the chat_history to the NVIDIA NIM chat completions endpoint with a prompt requesting structured JSON extraction
2. WHEN the NIM API returns a successful response, THE NIM_Analyzer SHALL extract the following fields from each conversation: outcome (one of: qualified_lead, not_interested, dropped_off, booked, spam, unclear), drop_off_stage (one of: greeting, qualification, objection_handling, closing, null if not applicable), sentiment (one of: positive, neutral, frustrated, negative), bot_error_detected (boolean), bot_error_notes (string of at most 500 characters, or null), notable_quote (string of at most 300 characters, or null), and summary (string of at most 200 characters providing a 1-2 sentence recap)
3. THE NIM_Analyzer SHALL use a configurable model name for the NIM API call
4. IF the NIM API returns a malformed response, THEN THE NIM_Analyzer SHALL retry once with a stricter instruction prompt that explicitly reiterates the expected JSON schema
5. IF the retry also returns a malformed response, THEN THE NIM_Analyzer SHALL log the failure including the conversation identifier and skip analysis for that conversation
6. IF the NIM API times out after 30 seconds or returns an error status, THEN THE NIM_Analyzer SHALL retry with exponential backoff starting at 2 seconds and doubling each attempt, up to 3 attempts, before logging the failure and skipping analysis for that conversation

### Requirement 5: Conversation State Persistence

**User Story:** As a system operator, I want all analyzed conversations stored with their structured output so that historical data is available for aggregation and review.

#### Acceptance Criteria

1. WHEN the NIM_Analyzer produces a Structured_Output, THE Memory_Store SHALL persist the output with the associated client_id, contact_id, timestamp, and dedupe_key
2. IF persistence of a Structured_Output fails, THEN THE Memory_Store SHALL retry once, and if the retry fails, log the error including the dedupe_key and skip aggregate updates for that conversation
3. THE Memory_Store SHALL maintain Rolling_Aggregates per client_id for daily lead volume, outcome distribution, drop-off rate by stage, and sentiment distribution over trailing 7-day and 30-day windows, where a day is defined as midnight-to-midnight UTC
4. WHEN a new Structured_Output is persisted, THE Memory_Store SHALL update the Rolling_Aggregates for the associated client_id within the same database transaction
5. THE Memory_Store SHALL retain raw payloads and Structured_Outputs for a minimum of 90 days; records older than 90 days may be purged

### Requirement 6: Real-Time Anomaly Detection

**User Story:** As a system operator, I want the system to detect anomalies after each conversation so that I am alerted to problems before they accumulate.

#### Acceptance Criteria

1. WHEN a new Structured_Output is persisted, THE Anomaly_Detector SHALL evaluate the current conversation against the Rolling_Aggregates for the associated client_id
2. THE Anomaly_Detector SHALL detect the following anomaly types using configurable thresholds: drop-off rate exceeding the rolling average by a configurable percentage (default 50%), 3 or more consecutive conversations with bot_error_detected flag set, lead volume falling below the rolling average for the current day and hour by a configurable percentage (default 50%), and 3 or more consecutive conversations with negative or frustrated sentiment scores
3. THE Anomaly_Detector SHALL require a configurable persistence count (default 3 occurrences, minimum 1, maximum 50) before triggering an alert for any anomaly type
4. THE Anomaly_Detector SHALL apply a Flag_Cooldown with a configurable duration (default 60 minutes) per combination of client_id, issue_type, and stage to prevent repeated alerts within the cooldown window
5. WHEN an anomaly meets the persistence threshold and is not within a cooldown period, THE Anomaly_Detector SHALL trigger a real-time alert via the Telegram_Notifier including the client_id, anomaly type, affected stage, current metric value, and rolling average value
6. IF the Rolling_Aggregates for a client_id contain fewer data points than the persistence count threshold, THEN THE Anomaly_Detector SHALL skip anomaly evaluation for that client_id and log that insufficient data is available

### Requirement 7: Inactive Hours Detection

**User Story:** As a system operator, I want the system to detect when no conversations arrive during expected active hours so that I am alerted to potential bot or platform outages.

#### Acceptance Criteria

1. THE Anomaly_Detector SHALL run a scheduled check at a configurable interval of no less than every 60 minutes to identify clients with zero conversations during their configured active hours
2. IF a client has received zero conversations during a configured active-hours window, THEN THE Anomaly_Detector SHALL trigger an alert via the Telegram_Notifier within 5 minutes of check completion, including the client identifier and the active-hours window that had no conversations
3. THE Anomaly_Detector SHALL use per-client configurable active-hours definitions that include a start time, end time, timezone, and applicable days of the week
4. IF a client does not have active-hours configured, THEN THE Anomaly_Detector SHALL skip that client during the scheduled check and not trigger an alert
5. IF the scheduled check fails to complete due to a dependency error, THEN THE Anomaly_Detector SHALL send a notification via the Telegram_Notifier indicating the check failure and the affected check cycle timestamp

### Requirement 8: Periodic Digest Generation

**User Story:** As a system operator, I want a daily digest summarizing conversation trends so that I can review performance without checking each alert individually.

#### Acceptance Criteria

1. THE Digest_Scheduler SHALL run at a configurable schedule (default daily, minimum interval 1 hour, maximum interval 7 days) using APScheduler
2. WHEN the digest schedule triggers, THE Digest_Scheduler SHALL retrieve all conversations and Rolling_Aggregates for each client_id since the last successful digest generation for that schedule
3. WHEN the digest schedule triggers, THE Digest_Scheduler SHALL send the retrieved aggregated data to the NIM API with a synthesis prompt requesting what is working, what is not, trending issues, and 1-2 actionable suggestions
4. WHEN the NIM API returns the synthesis, THE Digest_Scheduler SHALL format the output as a bulleted list with no more than 10 bullets per client section and no more than 280 characters per bullet, grouped with each client_id in its own labeled section, and deliver the digest via the Telegram_Notifier
5. IF the NIM API fails during digest generation, THEN THE Digest_Scheduler SHALL log the failure and retry at the next scheduled interval
6. IF a client_id has zero conversations in the digest period, THEN THE Digest_Scheduler SHALL omit that client from the digest rather than sending an empty section
7. IF the Telegram_Notifier fails to deliver the digest, THEN THE Digest_Scheduler SHALL log the failure and retry delivery on the next scheduled interval without regenerating the synthesis

### Requirement 9: Telegram Alert Delivery

**User Story:** As a system operator, I want immediate alerts sent to Telegram so that I can respond to anomalies quickly.

#### Acceptance Criteria

1. WHEN an anomaly alert is triggered, THE Telegram_Notifier SHALL send a message to the configured Telegram chat ID using the Bot API with a 🚨 prefix within 5 seconds of the anomaly being detected
2. THE Telegram_Notifier SHALL include the anomaly trigger type, client display name, the metric value that triggered the anomaly, and the baseline or threshold it exceeded in each alert message
3. IF the Telegram API returns an error, THEN THE Telegram_Notifier SHALL retry up to 3 times with a delay starting at 2 seconds and doubling after each attempt, and SHALL log the failure with the error details after all 3 retries are exhausted
4. IF the alert message exceeds 4096 characters, THEN THE Telegram_Notifier SHALL truncate the message to fit within the limit while preserving the prefix, trigger type, and client display name

### Requirement 10: Telegram Digest Delivery

**User Story:** As a system operator, I want periodic digests sent to Telegram so that I can review performance summaries in a convenient format.

#### Acceptance Criteria

1. WHEN a digest is generated, THE Telegram_Notifier SHALL send a message to the configured Telegram chat ID using the Bot API with a 📊 prefix
2. THE Telegram_Notifier SHALL structure each digest message with one section per client, labeled with the client display name
3. THE Telegram_Notifier SHALL format digest messages as bullet points with no more than 20 bullets per client and no more than 280 characters per bullet for readability
4. IF the digest message exceeds 4096 characters, THEN THE Telegram_Notifier SHALL split the digest into multiple sequential messages, each respecting the 4096-character limit
5. IF the Telegram API returns an error during digest delivery, THEN THE Telegram_Notifier SHALL retry up to 3 times with a delay starting at 2 seconds and doubling after each attempt, and SHALL log the failure after all retries are exhausted

### Requirement 11: Configuration Management

**User Story:** As a system operator, I want a centralized configuration so that I can manage multiple clients, credentials, and thresholds from a single file.

#### Acceptance Criteria

1. THE System SHALL load configuration from a config.yaml file and environment variables, where environment variables take precedence over config.yaml values for any overlapping keys
2. THE System SHALL support per-client configuration entries containing: client_id, display_name, alert thresholds (numeric values defining when alerts trigger), and active hours (start time and end time in 24-hour format with timezone)
3. THE System SHALL require the following global configuration values: webhook shared secret, NIM API key, NIM base URL, NIM model name, Telegram bot token, Telegram chat ID, digest schedule (cron expression), and alert sensitivity defaults (numeric threshold values)
4. IF a required configuration value is missing at startup, THEN THE System SHALL log an error identifying the missing value and refuse to start
5. IF the config.yaml file is unreadable or contains malformed YAML, THEN THE System SHALL log an error indicating the parse failure and refuse to start
6. IF a per-client entry is missing any of the required fields (client_id, display_name, alert thresholds, active hours), THEN THE System SHALL log an error identifying the incomplete client entry and refuse to start

### Requirement 12: Logging and Observability

**User Story:** As a system operator, I want structured logging throughout the system so that I can debug issues and audit processing.

#### Acceptance Criteria

1. THE System SHALL log each incoming webhook request with client_id, contact_id, and dedupe_key at INFO level
2. THE System SHALL log all NIM API calls including the request payload and response payload at DEBUG level, truncating any single log field value that exceeds 10,000 characters
3. THE System SHALL log all anomaly detections and alert deliveries at INFO level, including the contact_id and the detection outcome or delivery status
4. IF an error occurs in any module, THEN THE System SHALL log the error at ERROR level including the module name, contact_id (when available), error type, and error message
5. THE System SHALL output all log entries to stdout as structured JSON with each entry containing at minimum: an ISO 8601 timestamp, log level, module name, and a message field
6. THE System SHALL include the contact_id in all log entries associated with processing a specific contact so that an operator can trace a conversation from webhook receipt through analysis to alert delivery

### Requirement 13: Modular Architecture

**User Story:** As a developer, I want a modular codebase so that each concern (webhook reception, analysis, storage, alerting, scheduling) is independently maintainable and testable.

#### Acceptance Criteria

1. THE System SHALL organize code into separate Python modules (one file or package per concern): webhook receiver, NIM analysis, memory store, anomaly detection, digest scheduler, Telegram notification, and configuration loading, with no circular import dependencies between them
2. THE System SHALL provide an example .env and config.yaml with placeholder values covering all required configuration keys including: shared secret, Telegram bot token, Telegram chat ID, NIM API key, digest schedule interval, and any per-client identifiers
3. THE System SHALL include a README containing at minimum the following sections: deployment steps for Railway or Render, shared secret setup, Telegram bot configuration, NIM API key setup, onboarding new clients, and a description of each module's responsibility
4. WHEN any single module is imported in isolation, THE System SHALL allow that module to be exercised through its public interface without requiring the full application to be running
