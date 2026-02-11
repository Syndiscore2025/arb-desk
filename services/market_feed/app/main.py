"""
Market Feed Service - Live data ingestion with authenticated scraping.

This service manages credentialed logins to sportsbooks and scrapes odds data
using stealth browser automation. It pushes normalized odds to odds_ingest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, BackgroundTasks

from shared.schemas import (
    BetRequest,
    BetResponse,
    BookmakerCredentials,
    FeedConfig,
    FeedControlRequest,
    FeedControlResponse,
    FeedListResponse,
    FeedStatus,
    HealthResponse,
    MarketOdds,
    ProxyConfig,
    ScrapeResult,
    TwoFARequest,
    TwoFASubmission,
)
from shared.logging_config import setup_logging
from .session_manager import session_manager
from .bet_executor import BetExecutor

# Configure logging
logger = setup_logging("market_feed")
browser_logger = logging.getLogger("market_feed.browser")

# Environment configuration
SERVICE_NAME = os.getenv("SERVICE_NAME", "market_feed")
ODDS_INGEST_URL = os.getenv("ODDS_INGEST_URL", "http://odds_ingest:8000")
SLACK_NOTIFIER_URL = os.getenv("SLACK_NOTIFIER_URL", "http://slack_notifier:8000")

# Feed configurations from environment
FEED_CONFIGS_JSON = os.getenv("FEED_CONFIGS", "[]")
BOOKMAKER_CREDENTIALS_JSON = os.getenv("BOOKMAKER_CREDENTIALS", "{}")

# In-memory stores for Slack-based 2FA
_pending_2fa_requests: Dict[str, TwoFARequest] = {}
_submitted_2fa_codes: Dict[str, str] = {}

app = FastAPI(title="Market Feed", version="0.1.0")


def _load_feed_configs() -> List[FeedConfig]:
    """Load feed configurations from environment."""
    try:
        configs_data = json.loads(FEED_CONFIGS_JSON)
        return [FeedConfig(**cfg) for cfg in configs_data]
    except Exception as e:
        logger.error(f"Error loading feed configs: {e}")
        return []


def _load_credentials() -> Dict[str, BookmakerCredentials]:
    """Load bookmaker credentials from environment."""
    try:
        creds_data = json.loads(BOOKMAKER_CREDENTIALS_JSON)
        return {
            bm: BookmakerCredentials(bookmaker=bm, **cred)
            for bm, cred in creds_data.items()
        }
    except Exception as e:
        logger.error(f"Error loading credentials: {e}")
        return {}


@app.on_event("startup")
async def startup_event():
    """Initialize feeds on startup."""
    logger.info("Market Feed service starting...")
    
    configs = _load_feed_configs()
    credentials = _load_credentials()
    
    for config in configs:
        if config.bookmaker in credentials:
            session_manager.register_feed(config, credentials[config.bookmaker])
            logger.info(f"Registered feed: {config.bookmaker}")
        else:
            logger.warning(f"No credentials for {config.bookmaker}, skipping")
    
    logger.info(f"Initialized {len(session_manager._configs)} feeds")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up on shutdown."""
    logger.info("Market Feed service shutting down...")
    session_manager.close_all()


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Health check endpoint."""
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.get("/feeds", response_model=FeedListResponse)
def list_feeds() -> FeedListResponse:
    """List all configured feeds and their status."""
    statuses = session_manager.get_all_status()
    feeds = list(statuses.values())
    active = sum(1 for f in feeds if f.running)
    
    return FeedListResponse(
        feeds=feeds,
        active_count=active,
        total_count=len(feeds),
    )


@app.get("/feeds/{bookmaker}", response_model=FeedStatus)
def get_feed_status(bookmaker: str) -> FeedStatus:
    """Get status of a specific feed."""
    status = session_manager.get_status(bookmaker)
    if not status:
        raise HTTPException(status_code=404, detail=f"Feed not found: {bookmaker}")
    return status


@app.post("/feeds/control", response_model=FeedControlResponse)
def control_feed(request: FeedControlRequest) -> FeedControlResponse:
    """Control a feed (start, stop, restart)."""
    bookmaker = request.bookmaker
    action = request.action.lower()
    
    if bookmaker not in session_manager._configs:
        raise HTTPException(status_code=404, detail=f"Feed not found: {bookmaker}")
    
    try:
        if action == "start":
            success = session_manager.ensure_logged_in(bookmaker)
            message = "Feed started" if success else "Failed to start feed"
        elif action == "stop":
            adapter = session_manager._adapters.get(bookmaker)
            if adapter:
                adapter.close()
            success = True
            message = "Feed stopped"
        elif action == "restart":
            adapter = session_manager._adapters.get(bookmaker)
            if adapter:
                adapter.close()
            success = session_manager.ensure_logged_in(bookmaker)
            message = "Feed restarted" if success else "Failed to restart feed"
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
        
        return FeedControlResponse(
            bookmaker=bookmaker,
            action=action,
            success=success,
            message=message,
            status=session_manager.get_status(bookmaker),
        )
        
    except Exception as e:
        logger.error(f"Error controlling feed {bookmaker}: {e}")
        return FeedControlResponse(
            bookmaker=bookmaker,
            action=action,
            success=False,
            message=str(e),
        )


@app.post("/scrape/{bookmaker}", response_model=ScrapeResult)
def scrape_bookmaker(bookmaker: str) -> ScrapeResult:
    """
    Manually trigger a scrape for a specific bookmaker.
    This is a synchronous operation that returns the scraped odds.
    """
    if bookmaker not in session_manager._configs:
        raise HTTPException(status_code=404, detail=f"Feed not found: {bookmaker}")

    # Ensure logged in
    if not session_manager.ensure_logged_in(bookmaker):
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error="Failed to login",
        )

    # Perform scrape
    adapter = session_manager.get_adapter(bookmaker)
    if not adapter:
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error="Adapter not available",
        )

    return adapter.scrape()


@app.post("/scrape-and-push/{bookmaker}", response_model=ScrapeResult)
async def scrape_and_push(bookmaker: str) -> ScrapeResult:
    """
    Scrape a bookmaker and push results to odds_ingest.
    This is the main endpoint for live data ingestion.
    """
    # First scrape
    result = scrape_bookmaker(bookmaker)

    if not result.success or not result.odds:
        return result

    # Push to odds_ingest
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ODDS_INGEST_URL}/process",
                json=[odds.model_dump(mode="json") for odds in result.odds],
            )
            response.raise_for_status()
            logger.info(f"[{bookmaker}] Pushed {len(result.odds)} odds to odds_ingest")
    except Exception as e:
        logger.error(f"[{bookmaker}] Failed to push to odds_ingest: {e}")
        # Still return the scrape result, just log the push failure

    return result


@app.post("/scrape-all")
async def scrape_all_feeds() -> Dict[str, ScrapeResult]:
    """
    Scrape all enabled feeds and push results to odds_ingest.
    Returns results for each bookmaker.
    """
    results: Dict[str, ScrapeResult] = {}
    all_odds: List[MarketOdds] = []

    for bookmaker, config in session_manager._configs.items():
        if not config.enabled:
            continue

        try:
            result = scrape_bookmaker(bookmaker)
            results[bookmaker] = result

            if result.success and result.odds:
                all_odds.extend(result.odds)

        except Exception as e:
            logger.error(f"[{bookmaker}] Scrape error: {e}")
            results[bookmaker] = ScrapeResult(
                bookmaker=bookmaker,
                success=False,
                error=str(e),
            )

    # Push all odds to odds_ingest
    if all_odds:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{ODDS_INGEST_URL}/process",
                    json=[odds.model_dump(mode="json") for odds in all_odds],
                )
                response.raise_for_status()
                logger.info(f"Pushed {len(all_odds)} total odds to odds_ingest")
        except Exception as e:
            logger.error(f"Failed to push to odds_ingest: {e}")

    return results


@app.post("/register-feed")
def register_feed(config: FeedConfig, credentials: BookmakerCredentials) -> FeedStatus:
    """
    Dynamically register a new feed at runtime.
    Useful for adding feeds without restarting the service.
    """
    session_manager.register_feed(config, credentials)
    return session_manager.get_status(config.bookmaker)


# ─────────────────────────────────────────────────────────────────────────────
# Bet Execution
# ─────────────────────────────────────────────────────────────────────────────

# In-memory bet executors (created lazily per bookmaker)
_bet_executors: Dict[str, BetExecutor] = {}


@app.post("/bet/place", response_model=BetResponse)
async def place_bet(request: BetRequest) -> BetResponse:
    """
    Place a bet on a sportsbook.

    This is called by slack_notifier when a user responds to an arb alert
    with a stake amount. Uses the Playwright adapter for stealth bet placement.
    """
    bookmaker = request.bookmaker

    if bookmaker not in session_manager._configs:
        return BetResponse(
            bet_id=request.bet_id,
            bookmaker=bookmaker,
            success=False,
            error=f"Bookmaker {bookmaker} not configured",
        )

    # Ensure logged in
    if not session_manager.ensure_logged_in(bookmaker):
        return BetResponse(
            bet_id=request.bet_id,
            bookmaker=bookmaker,
            success=False,
            error="Failed to login to bookmaker",
        )

    # Get or create bet executor
    # Note: In production, this would use the Playwright adapter
    # For now, we return a placeholder response
    config = session_manager._configs[bookmaker]

    # Check if this is a Playwright adapter
    adapter = session_manager.get_adapter(bookmaker)
    if adapter is None:
        return BetResponse(
            bet_id=request.bet_id,
            bookmaker=bookmaker,
            success=False,
            error="Adapter not available",
        )

    # For now, log the bet request and return a simulated response
    # In production with Playwright adapters, we'd use BetExecutor
    logger.info(f"[{bookmaker}] Bet request: {request.selection} @ {request.odds_decimal:.2f} "
                f"for ${request.stake_amount:.2f}")

    # TODO: Implement actual bet placement with BetExecutor when using Playwright adapters
    # executor = BetExecutor(adapter, config)
    # return await executor.place_bet(request)

    return BetResponse(
        bet_id=request.bet_id,
        bookmaker=bookmaker,
        success=False,
        error="Bet execution not yet implemented for this adapter. "
              "Manual bet placement required.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Log Viewing Endpoints
# ─────────────────────────────────────────────────────────────────────────────

from pathlib import Path
from fastapi.responses import PlainTextResponse

LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/arb-desk"))


@app.get("/logs", response_class=PlainTextResponse)
async def get_logs(
    lines: int = 100,
    level: Optional[str] = None,
    bookmaker: Optional[str] = None,
) -> str:
    """
    Get recent log entries.

    Args:
        lines: Number of lines to return (default 100)
        level: Filter by log level (DEBUG, INFO, WARNING, ERROR)
        bookmaker: Filter by bookmaker name
    """
    log_file = LOG_DIR / "market_feed.log"

    if not log_file.exists():
        return "No logs available yet."

    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()

        # Get last N lines
        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines

        # Filter if requested
        if level or bookmaker:
            filtered = []
            for line in recent:
                try:
                    entry = json.loads(line)
                    if level and entry.get("level") != level.upper():
                        continue
                    if bookmaker and entry.get("bookmaker") != bookmaker:
                        continue
                    filtered.append(line)
                except json.JSONDecodeError:
                    continue
            recent = filtered

        return "".join(recent)

    except Exception as e:
        return f"Error reading logs: {str(e)}"


@app.get("/logs/browser", response_class=PlainTextResponse)
async def get_browser_logs(lines: int = 100) -> str:
    """Get browser-specific logs (navigation, scraping, errors)."""
    log_file = LOG_DIR / "browser.log"

    if not log_file.exists():
        return "No browser logs available yet."

    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()

        recent = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return "".join(recent)

    except Exception as e:
        return f"Error reading browser logs: {str(e)}"


@app.get("/logs/errors", response_class=PlainTextResponse)
async def get_error_logs(lines: int = 50) -> str:
    """Get only ERROR and CRITICAL level logs."""
    log_file = LOG_DIR / "market_feed.log"

    if not log_file.exists():
        return "No logs available yet."

    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()

        errors = []
        for line in all_lines:
            try:
                entry = json.loads(line)
                if entry.get("level") in ("ERROR", "CRITICAL"):
                    errors.append(line)
            except json.JSONDecodeError:
                continue

        recent = errors[-lines:] if len(errors) > lines else errors
        return "".join(recent) if recent else "No errors found."

    except Exception as e:
        return f"Error reading logs: {str(e)}"


@app.get("/logs/summary")
async def get_log_summary() -> Dict:
    """Get summary statistics of recent logs."""
    log_file = LOG_DIR / "market_feed.log"

    if not log_file.exists():
        return {"error": "No logs available yet"}

    try:
        with open(log_file, "r") as f:
            all_lines = f.readlines()

        # Count by level
        level_counts = {"DEBUG": 0, "INFO": 0, "WARNING": 0, "ERROR": 0, "CRITICAL": 0}
        bookmaker_counts: Dict[str, int] = {}
        event_counts: Dict[str, int] = {}
        recent_errors = []

        for line in all_lines[-1000:]:  # Last 1000 entries
            try:
                entry = json.loads(line)
                level = entry.get("level", "INFO")
                level_counts[level] = level_counts.get(level, 0) + 1

                if bm := entry.get("bookmaker"):
                    bookmaker_counts[bm] = bookmaker_counts.get(bm, 0) + 1

                if evt := entry.get("event_type"):
                    event_counts[evt] = event_counts.get(evt, 0) + 1

                if level in ("ERROR", "CRITICAL"):
                    recent_errors.append({
                        "timestamp": entry.get("timestamp"),
                        "message": entry.get("message"),
                        "bookmaker": entry.get("bookmaker"),
                    })

            except json.JSONDecodeError:
                continue

        return {
            "total_entries": len(all_lines),
            "level_counts": level_counts,
            "bookmaker_counts": bookmaker_counts,
            "event_counts": event_counts,
            "recent_errors": recent_errors[-10:],  # Last 10 errors
        }

    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Slack-Based 2FA Endpoints
# ─────────────────────────────────────────────────────────────────────────────


def _cleanup_expired_2fa_requests() -> None:
    """Remove expired 2FA requests from the pending store."""
    now = datetime.utcnow()
    expired_ids = [
        req_id for req_id, req in _pending_2fa_requests.items()
        if req.expires_at < now
    ]
    for req_id in expired_ids:
        _pending_2fa_requests[req_id].status = "expired"
        _pending_2fa_requests.pop(req_id, None)
        _submitted_2fa_codes.pop(req_id, None)


@app.post("/2fa/create")
async def create_2fa_request(payload: Dict) -> Dict:
    """
    Create a new 2FA request and notify user via Slack.

    Called by adapters when they detect a 2FA prompt on the page.
    """
    bookmaker = payload.get("bookmaker", "unknown")

    # Generate request ID
    request_id = str(uuid.uuid4())
    short_id = request_id[:8]

    # Create request with 5 minute expiry
    now = datetime.utcnow()
    request = TwoFARequest(
        request_id=request_id,
        bookmaker=bookmaker,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
        status="pending",
    )

    _pending_2fa_requests[request_id] = request

    # Send Slack notification
    try:
        slack_message = (
            f"🔐 *{bookmaker.upper()}* needs a 2FA code.\n"
            f"Check your phone for the SMS/email.\n"
            f"Reply with: `2fa {short_id} <code>`"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{SLACK_NOTIFIER_URL}/notify",
                json={"message": slack_message},
            )
        logger.info(f"[{bookmaker}] Created 2FA request {short_id}, notified Slack")
    except Exception as e:
        logger.error(f"[{bookmaker}] Failed to send Slack notification: {e}")

    return {"request_id": request_id, "short_id": short_id}


@app.get("/2fa/pending")
async def get_pending_2fa_requests() -> Dict:
    """Get all pending 2FA requests."""
    _cleanup_expired_2fa_requests()

    pending = [
        {
            "request_id": req.request_id,
            "short_id": req.request_id[:8],
            "bookmaker": req.bookmaker,
            "created_at": req.created_at.isoformat(),
            "expires_at": req.expires_at.isoformat(),
            "status": req.status,
        }
        for req in _pending_2fa_requests.values()
        if req.status == "pending"
    ]

    return {"pending": pending, "count": len(pending)}


@app.post("/2fa/submit")
async def submit_2fa_code(submission: TwoFASubmission) -> Dict:
    """
    Submit a 2FA code for a pending request.

    Called by slack_notifier when user provides a code via Slack.
    """
    _cleanup_expired_2fa_requests()

    # Find request by full ID or short prefix
    request_id = submission.request_id
    request = _pending_2fa_requests.get(request_id)

    if not request:
        # Try prefix match
        for req_id, req in _pending_2fa_requests.items():
            if req_id.startswith(request_id) or request_id.startswith(req_id[:8]):
                request = req
                request_id = req_id
                break

    if not request:
        return {"success": False, "error": f"Request {submission.request_id} not found"}

    if request.status != "pending":
        return {"success": False, "error": f"Request already {request.status}"}

    # Store the code and update status
    _submitted_2fa_codes[request_id] = submission.code
    request.status = "submitted"

    logger.info(f"[{request.bookmaker}] 2FA code submitted by {submission.submitted_by}")

    return {"success": True, "bookmaker": request.bookmaker}


@app.get("/2fa/check/{request_id}")
async def check_2fa_status(request_id: str) -> Dict:
    """Check the status of a 2FA request and retrieve submitted code if available."""
    _cleanup_expired_2fa_requests()

    # Find request by full ID or short prefix
    request = _pending_2fa_requests.get(request_id)

    if not request:
        for req_id, req in _pending_2fa_requests.items():
            if req_id.startswith(request_id) or request_id.startswith(req_id[:8]):
                request = req
                request_id = req_id
                break

    if not request:
        return {"status": "not_found", "code": None}

    code = _submitted_2fa_codes.get(request_id)

    return {
        "status": request.status,
        "code": code,
        "bookmaker": request.bookmaker,
    }


async def wait_for_2fa_code(request_id: str, timeout_seconds: int = 300) -> Optional[str]:
    """
    Poll for a 2FA code submission.

    Called by adapters after creating a 2FA request. Waits up to timeout_seconds
    for the user to submit a code via Slack.

    Args:
        request_id: The full UUID of the 2FA request
        timeout_seconds: Maximum time to wait (default 5 minutes)

    Returns:
        The submitted code, or None if timeout/expired
    """
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        if request_id in _submitted_2fa_codes:
            code = _submitted_2fa_codes.pop(request_id)
            _pending_2fa_requests.pop(request_id, None)
            return code
        await asyncio.sleep(2)

    # Timeout - mark as expired
    if request_id in _pending_2fa_requests:
        _pending_2fa_requests[request_id].status = "expired"
        _pending_2fa_requests.pop(request_id, None)

    return None
