"""
Market Feed Service - Live data ingestion with authenticated scraping.

This service manages credentialed logins to sportsbooks and scrapes odds data
using stealth browser automation. It pushes normalized odds to odds_ingest.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
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
)
from .session_manager import session_manager
from .bet_executor import BetExecutor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Environment configuration
SERVICE_NAME = os.getenv("SERVICE_NAME", "market_feed")
ODDS_INGEST_URL = os.getenv("ODDS_INGEST_URL", "http://odds_ingest:8000")

# Feed configurations from environment
FEED_CONFIGS_JSON = os.getenv("FEED_CONFIGS", "[]")
BOOKMAKER_CREDENTIALS_JSON = os.getenv("BOOKMAKER_CREDENTIALS", "{}")

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

