from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, HttpUrl


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str
    time_utc: datetime = Field(default_factory=datetime.utcnow)


class MarketOdds(BaseModel):
    event_id: str
    sport: str
    market: str
    bookmaker: str
    selection: str
    odds_decimal: float = Field(gt=1.0)
    captured_at: datetime = Field(default_factory=datetime.utcnow)


class OddsIngestResponse(BaseModel):
    accepted: int
    dropped: int = 0
    received_at: datetime = Field(default_factory=datetime.utcnow)


class ArbOpportunity(BaseModel):
    event_id: str
    market: str
    implied_prob_sum: float
    has_arb: bool
    notes: Optional[str] = None


class ArbRequest(BaseModel):
    odds: List[MarketOdds]


class ArbResponse(BaseModel):
    opportunities: List[ArbOpportunity]
    evaluated_at: datetime = Field(default_factory=datetime.utcnow)


class ObserveRequest(BaseModel):
    url: HttpUrl
    user_agent: Optional[str] = None


class ObserveResponse(BaseModel):
    url: HttpUrl
    final_url: HttpUrl
    title: Optional[str] = None
    fetched_at: datetime = Field(default_factory=datetime.utcnow)


class DecisionRequest(BaseModel):
    opportunity: ArbOpportunity
    context: Dict[str, Any] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    decision: str
    rationale: str
    decided_at: datetime = Field(default_factory=datetime.utcnow)


class SlackNotification(BaseModel):
    message: str
    channel: Optional[str] = None
    username: Optional[str] = None


class SlackNotificationResponse(BaseModel):
    delivered: bool
    detail: Optional[str] = None
    sent_at: datetime = Field(default_factory=datetime.utcnow)