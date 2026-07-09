"""Pydantic data models for the conversation intelligence monitor."""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Outcome(str, Enum):
    """Possible conversation outcomes."""

    QUALIFIED_LEAD = "qualified_lead"
    NOT_INTERESTED = "not_interested"
    DROPPED_OFF = "dropped_off"
    BOOKED = "booked"
    SPAM = "spam"
    UNCLEAR = "unclear"


class DropOffStage(str, Enum):
    """Stages at which a conversation can drop off."""

    GREETING = "greeting"
    QUALIFICATION = "qualification"
    OBJECTION_HANDLING = "objection_handling"
    CLOSING = "closing"


class Sentiment(str, Enum):
    """Detected sentiment of the lead/user."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    FRUSTRATED = "frustrated"
    NEGATIVE = "negative"


class ChatMessage(BaseModel):
    """A single message in a conversation history."""

    role: str
    content: str
    timestamp: Optional[str] = None


class WebhookPayload(BaseModel):
    """Validated incoming webhook payload."""

    contact_id: str
    timestamp: str  # ISO 8601
    chat_history: list[ChatMessage]
    tags: Optional[list[str]] = None
    last_ref: Optional[str] = None
    user_source: Optional[str] = None


class StructuredOutput(BaseModel):
    """NIM analysis result for a single conversation."""

    outcome: Outcome
    drop_off_stage: Optional[DropOffStage] = None
    sentiment: Sentiment
    bot_error_detected: bool
    bot_error_notes: Optional[str] = Field(None, max_length=500)
    notable_quote: Optional[str] = Field(None, max_length=300)
    summary: str = Field(..., max_length=200)


class AlertThresholds(BaseModel):
    """Per-client or default alert configuration."""

    dropoff_rate_pct: float = 50.0
    low_volume_pct: float = 50.0
    consecutive_errors: int = 3
    consecutive_neg_sentiment: int = 3
    persistence_count: int = Field(default=3, ge=1, le=50)
    cooldown_minutes: int = 60


class ActiveHours(BaseModel):
    """Per-client active hours definition."""

    start_time: str  # HH:MM 24h format
    end_time: str  # HH:MM 24h format
    timezone: str  # e.g. "America/Sao_Paulo"
    days: list[int]  # 0=Monday..6=Sunday


class AnomalyAlert(BaseModel):
    """A detected anomaly ready for notification."""

    client_id: str
    client_display_name: str
    issue_type: str  # "high_dropoff" | "consecutive_errors" | "low_volume" | "negative_sentiment" | "inactive_hours"
    stage: Optional[str] = None
    metric_value: float
    baseline_value: float
    message: str


class DigestSection(BaseModel):
    """One client's section in a digest."""

    client_id: str
    client_display_name: str
    bullets: list[str]  # max 10 items, max 280 chars each


class DigestMessage(BaseModel):
    """Complete digest with all client sections."""

    sections: list[DigestSection]
    generated_at: datetime


class RollingAggregates(BaseModel):
    """Computed rolling statistics for a client."""

    daily_volume_7d: list[int] = Field(default_factory=list)
    daily_volume_30d: list[int] = Field(default_factory=list)
    outcome_dist_7d: dict[str, int] = Field(default_factory=dict)
    outcome_dist_30d: dict[str, int] = Field(default_factory=dict)
    dropoff_by_stage_7d: dict[str, int] = Field(default_factory=dict)
    dropoff_by_stage_30d: dict[str, int] = Field(default_factory=dict)
    sentiment_dist_7d: dict[str, int] = Field(default_factory=dict)
    sentiment_dist_30d: dict[str, int] = Field(default_factory=dict)
    recent_errors: list[bool] = Field(default_factory=list)  # last N bot_error_detected values
    recent_sentiments: list[str] = Field(default_factory=list)  # last N sentiment values
    total_conversations_7d: int = 0
    total_conversations_30d: int = 0
