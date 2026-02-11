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
    # Enhanced market type info
    market_type: str = "moneyline"  # moneyline, spread, total, prop, future, parlay, boost
    is_live: bool = False
    is_boosted: bool = False  # True if this is a boosted/promoted odd
    original_odds: Optional[float] = None  # Pre-boost odds if boosted
    line: Optional[float] = None  # For spreads/totals (e.g., -3.5, 45.5)
    player_name: Optional[str] = None  # For player props
    prop_type: Optional[str] = None  # points, rebounds, assists, strikeouts, etc.
    period: Optional[str] = None  # full_game, first_half, first_quarter, etc.
    expires_at: Optional[datetime] = None  # For futures/promos with expiration


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
    # Enhanced arb details
    profit_percentage: Optional[float] = None  # e.g., 2.5 for 2.5% profit
    legs: List[Dict[str, Any]] = Field(default_factory=list)  # Each leg with bookmaker, selection, odds
    is_live: bool = False  # True if this is a live/in-play arb
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    expires_estimate_seconds: Optional[int] = None  # Estimated time before odds change


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


# ─────────────────────────────────────────────────────────────────────────────
# Market Feed Schemas
# ─────────────────────────────────────────────────────────────────────────────


class TwoFactorConfig(BaseModel):
    """Configuration for 2FA code retrieval."""
    method: str  # "totp", "sms", "email", "slack"
    # For TOTP
    totp_secret: Optional[str] = None
    # For SMS/Email API
    api_url: Optional[str] = None  # API endpoint to fetch code
    api_key: Optional[str] = None  # API key for authentication
    api_headers: Optional[Dict[str, str]] = None  # Custom headers
    phone_number: Optional[str] = None  # For SMS services
    email_address: Optional[str] = None  # For email services
    code_regex: Optional[str] = None  # Regex to extract code from response
    poll_interval_seconds: int = 2  # How often to poll for code
    poll_timeout_seconds: int = 60  # Max time to wait for code


class TwoFARequest(BaseModel):
    """A pending 2FA request waiting for user input via Slack."""
    request_id: str  # Full UUID
    bookmaker: str
    created_at: datetime
    expires_at: datetime  # 5 minutes from creation
    status: str = "pending"  # pending, submitted, expired


class TwoFASubmission(BaseModel):
    """A 2FA code submission from Slack."""
    request_id: str  # Can be short prefix (first 8 chars) or full UUID
    code: str  # The 2FA code (4-8 digits)
    submitted_by: str  # Slack user ID


class BookmakerCredentials(BaseModel):
    """Credentials for a single sportsbook."""
    bookmaker: str
    username: str
    password: str
    totp_secret: Optional[str] = None  # For simple TOTP (backward compat)
    two_factor: Optional[TwoFactorConfig] = None  # Advanced 2FA config


class ProxyConfig(BaseModel):
    """Proxy configuration for stealth browsing."""
    host: str
    port: int
    username: Optional[str] = None
    password: Optional[str] = None
    protocol: str = "http"  # http, https, socks5


class FeedConfig(BaseModel):
    """Configuration for a market feed adapter."""
    bookmaker: str
    enabled: bool = True
    poll_interval_seconds: int = Field(default=10, ge=5, le=60)
    min_delay_seconds: float = Field(default=2.0, ge=1.0)
    max_delay_seconds: float = Field(default=10.0, le=30.0)
    max_retries: int = Field(default=3, ge=1)
    login_url: Optional[str] = None
    odds_urls: List[str] = Field(default_factory=list)
    live_odds_urls: List[str] = Field(default_factory=list)  # In-play/live URLs
    sports: List[str] = Field(default_factory=list)
    markets: List[str] = Field(default_factory=list)
    proxy: Optional[ProxyConfig] = None
    headless: bool = True
    extra_config: Dict[str, Any] = Field(default_factory=dict)
    # Live/in-play polling settings
    live_polling_enabled: bool = False
    live_poll_interval_seconds: int = Field(default=5, ge=3, le=15)  # Fast polling for live
    # Multi-account support
    credential_rotation_enabled: bool = False  # For single-login books like DraftKings


class SessionStatus(BaseModel):
    """Status of a browser session."""
    bookmaker: str
    logged_in: bool = False
    last_login_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    login_failures: int = 0
    session_valid: bool = False
    error: Optional[str] = None


class FeedStatus(BaseModel):
    """Overall status of a feed adapter."""
    bookmaker: str
    enabled: bool
    running: bool = False
    session: SessionStatus
    last_scrape_at: Optional[datetime] = None
    scrape_count: int = 0
    error_count: int = 0
    last_error: Optional[str] = None
    odds_collected: int = 0


class ScrapeResult(BaseModel):
    """Result of a single scrape operation."""
    bookmaker: str
    success: bool
    odds: List[MarketOdds] = Field(default_factory=list)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    duration_ms: int = 0
    error: Optional[str] = None
    page_url: Optional[str] = None


class FeedControlRequest(BaseModel):
    """Request to control a feed (start/stop/restart)."""
    bookmaker: str
    action: str  # start, stop, restart, scrape_now


class FeedControlResponse(BaseModel):
    """Response from feed control action."""
    bookmaker: str
    action: str
    success: bool
    message: str
    status: Optional[FeedStatus] = None


class FeedListResponse(BaseModel):
    """List of all configured feeds and their status."""
    feeds: List[FeedStatus]
    active_count: int
    total_count: int


# ─────────────────────────────────────────────────────────────────────────────
# Bet Execution Schemas
# ─────────────────────────────────────────────────────────────────────────────


class BetLeg(BaseModel):
    """A single leg of a bet."""
    bookmaker: str
    event_id: str
    selection: str
    odds_decimal: float
    market: str
    sport: str


class BetRequest(BaseModel):
    """Request to place a bet on a sportsbook."""
    bet_id: str  # Unique ID for tracking
    bookmaker: str
    event_id: str
    selection: str
    odds_decimal: float
    stake_amount: float  # Amount to bet
    market: str
    sport: str
    arb_opportunity_id: Optional[str] = None  # Link to arb opportunity


class BetResponse(BaseModel):
    """Response from bet placement."""
    bet_id: str
    bookmaker: str
    success: bool
    confirmation_number: Optional[str] = None
    actual_odds: Optional[float] = None  # Odds at time of placement (may differ)
    actual_stake: Optional[float] = None
    potential_payout: Optional[float] = None
    error: Optional[str] = None
    placed_at: datetime = Field(default_factory=datetime.utcnow)


class ArbAlert(BaseModel):
    """Alert for an arbitrage opportunity sent to Slack."""
    alert_id: str
    opportunity: ArbOpportunity
    tier: str  # "fire" (>3%), "lightning" (1.5-3%), "info" (<1.5%)
    message: str
    deep_links: Dict[str, str] = Field(default_factory=dict)  # bookmaker -> URL
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: Optional[datetime] = None
    status: str = "pending"  # pending, accepted, rejected, expired


class BetCommand(BaseModel):
    """Command from user to place a bet (via Slack reply)."""
    alert_id: str
    stake_amount: float
    user_id: str  # Slack user ID
    received_at: datetime = Field(default_factory=datetime.utcnow)


class MultiAccountCredentials(BaseModel):
    """Multiple credential sets for a single bookmaker (for rotation)."""
    bookmaker: str
    credentials: List[BookmakerCredentials]
    active_index: int = 0  # Currently active credential set
    rotation_on_logout: bool = True  # Rotate when logged out by book