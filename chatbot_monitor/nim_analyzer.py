"""NVIDIA NIM API client for conversation analysis.

Implements:
- OpenAI-compatible chat completions endpoint (POST {base_url}/chat/completions)
- Retry with stricter prompt on malformed response (1 retry)
- Exponential backoff (2s→4s→8s) for timeout/HTTP errors (3 retries)
- Circuit breaker: 5 consecutive failures → 5-minute cooldown
- Configurable model name, base URL, and API key from AppConfig
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from chatbot_monitor.config import AppConfig
from chatbot_monitor.models import StructuredOutput

logger = logging.getLogger(__name__)

# Directory containing prompt templates
_PROMPTS_DIR = Path(__file__).parent / "prompts"

# Circuit breaker settings
_CIRCUIT_BREAKER_THRESHOLD = 5  # consecutive failures to open circuit
_CIRCUIT_BREAKER_COOLDOWN_SECONDS = 300  # 5 minutes

# Retry settings
_HTTP_MAX_RETRIES = 3
_HTTP_BACKOFF_BASE = 2  # seconds; doubles each retry: 2s → 4s → 8s
_MALFORMED_MAX_RETRIES = 1  # one retry with stricter prompt

# Request timeout
_REQUEST_TIMEOUT_SECONDS = 30

# Logging truncation
_LOG_TRUNCATE_LENGTH = 10_000


def _truncate_for_log(text: str) -> str:
    """Truncate text to 10k chars for log entries."""
    if len(text) > _LOG_TRUNCATE_LENGTH:
        return text[:_LOG_TRUNCATE_LENGTH] + "...[TRUNCATED]"
    return text


def _load_prompt(filename: str) -> str:
    """Load a prompt template from the prompts directory."""
    prompt_path = _PROMPTS_DIR / filename
    return prompt_path.read_text(encoding="utf-8")


def _format_chat_history(chat_history: list[dict]) -> str:
    """Format chat_history into a readable text representation for the prompt."""
    lines = []
    for msg in chat_history:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        prefix = f"[{timestamp}] " if timestamp else ""
        lines.append(f"{prefix}{role}: {content}")
    return "\n".join(lines)


class NIMAnalyzer:
    """NVIDIA NIM API client for conversation analysis.

    Uses the OpenAI-compatible chat completions endpoint to extract
    structured insights from WhatsApp conversation transcripts.

    Features:
    - Malformed response retry: retries once with a stricter prompt
    - HTTP error/timeout retry: exponential backoff (2s→4s→8s), 3 attempts
    - Circuit breaker: after 5 consecutive failures, enters 5-minute cooldown
    """

    def __init__(self, config: AppConfig, http_client: httpx.AsyncClient):
        self._config = config
        self._http_client = http_client
        self._base_url = config.nim_base_url.rstrip("/")
        self._model = config.nim_model
        self._api_key = config.nim_api_key

        # Circuit breaker state
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0  # timestamp when circuit can close

        # Load prompt templates
        self._analysis_prompt = _load_prompt("analysis.txt")
        self._analysis_strict_prompt = _load_prompt("analysis_strict.txt")

    @property
    def circuit_is_open(self) -> bool:
        """Check if the circuit breaker is currently open (in cooldown)."""
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            if time.time() < self._circuit_open_until:
                return True
            # Cooldown expired — half-open state, allow one attempt
        return False

    def _record_success(self) -> None:
        """Record a successful call, resetting the circuit breaker."""
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Record a failed call, potentially opening the circuit breaker."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CIRCUIT_BREAKER_THRESHOLD:
            self._circuit_open_until = time.time() + _CIRCUIT_BREAKER_COOLDOWN_SECONDS
            logger.warning(
                "Circuit breaker OPEN: %d consecutive NIM failures, "
                "cooldown for %d seconds",
                self._consecutive_failures,
                _CIRCUIT_BREAKER_COOLDOWN_SECONDS,
            )

    async def analyze(
        self, chat_history: list[dict], client_id: str, dedupe_key: str
    ) -> StructuredOutput | None:
        """Send chat_history to NIM, return structured output or None on failure.

        Retry strategy:
        - Malformed response: retry once with stricter prompt (immediate)
        - Timeout/HTTP error: exponential backoff 2s→4s→8s (3 attempts)
        - Circuit breaker: after 5 consecutive failures, skip for 5 minutes

        Args:
            chat_history: List of message dicts with role/content/timestamp.
            client_id: The client identifier for logging.
            dedupe_key: Unique conversation key for logging.

        Returns:
            StructuredOutput on success, None on failure after all retries.
        """
        # Circuit breaker check
        if self.circuit_is_open:
            logger.warning(
                "Circuit breaker is OPEN, skipping NIM call",
                extra={"client_id": client_id, "dedupe_key": dedupe_key},
            )
            return None

        formatted_history = _format_chat_history(chat_history)

        # First attempt with standard prompt
        result = await self._attempt_with_retries(
            formatted_history, client_id, dedupe_key, strict=False
        )

        if isinstance(result, StructuredOutput):
            self._record_success()
            return result

        # If we got a malformed response (not a transport error), retry with strict prompt
        if result == "malformed":
            logger.info(
                "Malformed NIM response, retrying with stricter prompt",
                extra={"client_id": client_id, "dedupe_key": dedupe_key},
            )
            strict_result = await self._attempt_with_retries(
                formatted_history, client_id, dedupe_key, strict=True
            )
            if isinstance(strict_result, StructuredOutput):
                self._record_success()
                return strict_result

            # Strict retry also failed
            self._record_failure()
            logger.error(
                "NIM analysis failed after strict retry",
                extra={"client_id": client_id, "dedupe_key": dedupe_key},
            )
            return None

        # Transport error — already retried with backoff inside _attempt_with_retries
        self._record_failure()
        logger.error(
            "NIM analysis failed after all retries (transport/HTTP error)",
            extra={"client_id": client_id, "dedupe_key": dedupe_key},
        )
        return None

    async def _attempt_with_retries(
        self,
        formatted_history: str,
        client_id: str,
        dedupe_key: str,
        strict: bool,
    ) -> StructuredOutput | str:
        """Attempt NIM call with exponential backoff on HTTP/timeout errors.

        Returns:
            StructuredOutput on success.
            "malformed" if response was received but couldn't be parsed.
            "error" if all retries exhausted due to transport/HTTP errors.
        """
        prompt_template = (
            self._analysis_strict_prompt if strict else self._analysis_prompt
        )
        system_prompt = prompt_template.replace("{chat_history}", "").strip()
        # The chat_history is sent as the user message content
        user_content = formatted_history

        for attempt in range(_HTTP_MAX_RETRIES):
            try:
                response_text = await self._call_nim(
                    system_prompt, user_content, client_id, dedupe_key
                )

                # Try to parse the response
                parsed = self._parse_response(response_text)
                if parsed is not None:
                    return parsed

                # Malformed response — don't retry with backoff, signal to caller
                return "malformed"

            except (httpx.TimeoutException, httpx.HTTPStatusError) as e:
                if attempt < _HTTP_MAX_RETRIES - 1:
                    backoff = _HTTP_BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "NIM call failed (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1,
                        _HTTP_MAX_RETRIES,
                        backoff,
                        str(e),
                        extra={"client_id": client_id, "dedupe_key": dedupe_key},
                    )
                    await asyncio.sleep(backoff)
                else:
                    logger.error(
                        "NIM call failed after %d attempts: %s",
                        _HTTP_MAX_RETRIES,
                        str(e),
                        extra={"client_id": client_id, "dedupe_key": dedupe_key},
                    )
                    return "error"

            except Exception as e:
                # Unexpected error — treat as transport failure
                logger.error(
                    "Unexpected error during NIM call: %s",
                    str(e),
                    extra={"client_id": client_id, "dedupe_key": dedupe_key},
                )
                if attempt < _HTTP_MAX_RETRIES - 1:
                    backoff = _HTTP_BACKOFF_BASE * (2**attempt)
                    await asyncio.sleep(backoff)
                else:
                    return "error"

        return "error"

    async def _call_nim(
        self,
        system_prompt: str,
        user_content: str,
        client_id: str,
        dedupe_key: str,
    ) -> str:
        """Make a single HTTP call to the NIM chat completions endpoint.

        Raises:
            httpx.TimeoutException: On request timeout (30s).
            httpx.HTTPStatusError: On non-2xx response status.

        Returns:
            The response content text from the NIM API.
        """
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }

        logger.debug(
            "NIM request: %s",
            _truncate_for_log(json.dumps(payload, ensure_ascii=False)),
            extra={"client_id": client_id, "dedupe_key": dedupe_key},
        )

        response = await self._http_client.post(
            url,
            json=payload,
            headers=headers,
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

        response_text = response.text
        logger.debug(
            "NIM response: %s",
            _truncate_for_log(response_text),
            extra={"client_id": client_id, "dedupe_key": dedupe_key},
        )

        return response_text

    def _parse_response(self, response_text: str) -> StructuredOutput | None:
        """Parse NIM response into StructuredOutput.

        Handles both raw JSON responses and OpenAI-compatible chat completion
        responses (where the content is in choices[0].message.content).

        Returns:
            StructuredOutput if parsing succeeds, None if malformed.
        """
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            logger.warning("NIM response is not valid JSON")
            return None

        # OpenAI-compatible format: extract content from choices
        content_str = None
        if "choices" in data and isinstance(data["choices"], list):
            if len(data["choices"]) > 0:
                message = data["choices"][0].get("message", {})
                content_str = message.get("content", "")
        elif all(
            k in data
            for k in ("outcome", "sentiment", "bot_error_detected", "summary")
        ):
            # Direct JSON object (unlikely but handle gracefully)
            content_str = response_text
        else:
            logger.warning("NIM response has unexpected structure")
            return None

        if not content_str:
            logger.warning("NIM response has empty content")
            return None

        # Parse the content string as JSON
        try:
            # Handle potential markdown code block wrapping
            clean_content = content_str.strip()
            if clean_content.startswith("```"):
                # Remove markdown code fence
                lines = clean_content.split("\n")
                # Remove first line (```json or ```)
                lines = lines[1:]
                # Remove last line if it's closing fence
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                clean_content = "\n".join(lines).strip()

            output_data = json.loads(clean_content)
        except json.JSONDecodeError:
            logger.warning(
                "NIM response content is not valid JSON: %s",
                _truncate_for_log(content_str),
            )
            return None

        # Validate against StructuredOutput schema
        try:
            return StructuredOutput.model_validate(output_data)
        except Exception as e:
            logger.warning(
                "NIM response does not match StructuredOutput schema: %s",
                str(e),
            )
            return None
