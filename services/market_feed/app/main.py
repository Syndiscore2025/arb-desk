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
import random
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

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
from .adapters.prediction_markets import (
    KalshiAdapter,
    PolymarketAdapter,
    PredictionMarketEventUnifier,
)
from .prediction_market_diagnostics import categorize_prediction_market_titles
from .prediction_market_runtime import PredictionMarketRuntimeStore

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

# ─────────────────────────────────────────────────────────────────────────────
# Auto-Polling State
# ─────────────────────────────────────────────────────────────────────────────
_poller_tasks: Dict[str, asyncio.Task] = {}   # bookmaker -> background Task
_poller_stats: Dict[str, Dict] = {}           # bookmaker -> runtime stats

# Bookmakers whose auto-pollers are intentionally disabled (bookmaker -> reason)
# This is used to keep the service healthy/clean when a required upstream
# dependency (e.g., ODDS_API_KEY) is missing/invalid.
_disabled_pollers: Dict[str, str] = {}

# Track prediction-market adapter instances so we can close HTTP clients on shutdown
_prediction_market_adapters: List[object] = []
_prediction_market_runtime_store = PredictionMarketRuntimeStore(
    history_limit=int(os.getenv("PREDICTION_MARKET_HISTORY_LIMIT", "25")),
    payload_sample_size=int(os.getenv("PREDICTION_MARKET_PAYLOAD_SAMPLE_SIZE", "25")),
)

# Randomized polling bounds (seconds)
POLL_MIN_INTERVAL = int(os.getenv("POLL_MIN_INTERVAL", "45"))
POLL_MAX_INTERVAL = int(os.getenv("POLL_MAX_INTERVAL", "90"))
# Stagger offset so bookmakers don't start at the same instant
POLL_STAGGER_MAX = int(os.getenv("POLL_STAGGER_MAX", "30"))

app = FastAPI(title="Market Feed", version="0.1.0")


async def _validate_odds_api_key(api_key: str) -> bool:
    """Best-effort validation of ODDS_API_KEY.

    We only use this to decide whether to *disable sportsbook auto-pollers*.
    If validation fails for non-auth reasons (network/timeouts/etc), we do NOT
    disable anything.
    """
    try:
        # Import locally to avoid changing import order / startup side-effects
        from .adapters.odds_api_adapter import ODDS_API_BASE_URL, SPORT_KEYS

        sport_key = SPORT_KEYS.get("nfl") or "americanfootball_nfl"
        url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": api_key,
            "regions": "us",
            "markets": "h2h",
            "bookmakers": "draftkings",
            "oddsFormat": "decimal",
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)

        if resp.status_code in (401, 403):
            return False

        # Treat other statuses as "unknown but not definitively invalid".
        return True
    except Exception as exc:
        logger.warning(f"[odds_api] Key validation skipped (error): {exc}")
        return True


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


def _prediction_market_adapter_state(adapter: object) -> Optional[Dict[str, Any]]:
    snapshotter = getattr(adapter, "runtime_state_snapshot", None)
    if not callable(snapshotter):
        return None
    try:
        snapshot = snapshotter()
        return snapshot if isinstance(snapshot, dict) else None
    except Exception as exc:
        return {"name": adapter.__class__.__name__.lower(), "snapshot_error": str(exc)[:200]}


def _prediction_market_adapter_states() -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for adapter in list(_prediction_market_adapters):
        snapshot = _prediction_market_adapter_state(adapter)
        if not snapshot:
            continue
        name = str(snapshot.get("name") or adapter.__class__.__name__.lower())
        snapshots[name] = snapshot
    return snapshots


def _record_prediction_market_cycle(
    market_name: str,
    *,
    status: str,
    source_counts: Dict[str, int],
    batch_count: int,
    pushed: bool,
    duration_seconds: float,
    adapter_states: Optional[Dict[str, Dict[str, Any]]] = None,
    matching: Optional[Dict[str, Any]] = None,
    fetch_errors: Optional[List[str]] = None,
    error: Optional[str] = None,
    payload: Optional[List[Dict[str, Any]]] = None,
) -> None:
    stats = _poller_stats.setdefault(market_name, {})
    stats.update(
        {
            "last_cycle_status": status,
            "last_batch_count": int(batch_count),
            "last_source_counts": dict(source_counts),
            "last_push_at": datetime.utcnow().isoformat() if pushed else stats.get("last_push_at"),
            "last_duration_seconds": round(float(duration_seconds), 3),
            "last_fetch_errors": list(fetch_errors or []),
            "last_matching": dict(matching or {}),
            "last_error": str(error)[:200] if error else stats.get("last_error"),
        }
    )
    _prediction_market_runtime_store.record_cycle(
        market_name=market_name,
        status=status,
        source_counts=source_counts,
        batch_count=batch_count,
        pushed=pushed,
        duration_seconds=duration_seconds,
        adapter_states=adapter_states,
        matching=matching,
        fetch_errors=fetch_errors,
        error=error,
        payload=payload,
    )


def _prediction_market_enabled_config() -> Dict[str, bool]:
    poly_enabled = os.getenv("POLYMARKET_ENABLED", "false").lower() == "true"
    kalshi_enabled = os.getenv("KALSHI_ENABLED", "false").lower() == "true"
    return {
        "polymarket": poly_enabled,
        "kalshi": kalshi_enabled,
        "combined": poly_enabled and kalshi_enabled,
    }


def _prediction_market_pollers_status() -> Dict[str, Dict[str, Any]]:
    pollers: Dict[str, Dict[str, Any]] = {}
    for name in ("polymarket", "kalshi", "prediction_markets"):
        if name not in _poller_tasks and name not in _poller_stats:
            continue
        task = _poller_tasks.get(name)
        pollers[name] = {
            "running": bool(task and not task.done()),
            **_poller_stats.get(name, {}),
        }
    return pollers


# ─────────────────────────────────────────────────────────────────────────────
# Auto-Polling Background Loop
# ─────────────────────────────────────────────────────────────────────────────

async def _auto_poll_loop(bookmaker: str) -> None:
    """
    Continuous background polling loop for a single bookmaker.

    Uses randomized intervals between POLL_MIN_INTERVAL and POLL_MAX_INTERVAL
    so that requests don't follow a fixed cadence (anti-detection).
    Each bookmaker runs its own independent loop with different timing.
    """
    _poller_stats[bookmaker] = {
        "started_at": datetime.utcnow().isoformat(),
        "poll_count": 0,
        "success_count": 0,
        "error_count": 0,
        "last_poll_at": None,
        "last_error": None,
        "next_delay": None,
    }

    # Stagger start — each bookmaker waits a random offset so they
    # don't hit their respective sites at the same second.
    stagger = random.uniform(5, POLL_STAGGER_MAX)
    logger.info(f"[{bookmaker}] Auto-poller starting in {stagger:.1f}s (staggered)")
    await asyncio.sleep(stagger)

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10
    BASE_ERROR_BACKOFF = 30  # seconds

    while True:
        poll_start = datetime.utcnow()
        _poller_stats[bookmaker]["poll_count"] += 1
        _poller_stats[bookmaker]["last_poll_at"] = poll_start.isoformat()

        try:
            # Ensure logged in first
            logged_in = session_manager.ensure_logged_in(bookmaker)
            if not logged_in:
                logger.warning(f"[{bookmaker}] Not logged in — skipping poll cycle")
                _poller_stats[bookmaker]["error_count"] += 1
                _poller_stats[bookmaker]["last_error"] = "not_logged_in"
                consecutive_errors += 1
            else:
                # Scrape
                adapter = session_manager.get_adapter(bookmaker)
                if adapter is None:
                    raise RuntimeError("Adapter not available")

                result: ScrapeResult = adapter.scrape()

                if not result.success:
                    raise RuntimeError(result.error or "scrape failed")

                # Push odds to odds_ingest (triggers arb_math → decision_gateway → slack)
                if result.odds:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(
                            f"{ODDS_INGEST_URL}/process",
                            json=[o.model_dump(mode="json") for o in result.odds],
                        )
                        resp.raise_for_status()
                    logger.info(
                        f"[{bookmaker}] Auto-poll OK — "
                        f"{len(result.odds)} odds pushed to pipeline"
                    )
                else:
                    logger.info(f"[{bookmaker}] Auto-poll OK — 0 odds (empty scrape)")

                _poller_stats[bookmaker]["success_count"] += 1
                consecutive_errors = 0  # reset on success

        except asyncio.CancelledError:
            logger.info(f"[{bookmaker}] Auto-poller cancelled")
            return
        except Exception as exc:
            consecutive_errors += 1
            _poller_stats[bookmaker]["error_count"] += 1
            _poller_stats[bookmaker]["last_error"] = str(exc)[:200]
            logger.error(f"[{bookmaker}] Auto-poll error ({consecutive_errors}): {exc}")

        # --- Determine next sleep ---
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            # Exponential-ish back-off capped at 10 minutes
            backoff = min(BASE_ERROR_BACKOFF * (2 ** (consecutive_errors - MAX_CONSECUTIVE_ERRORS)), 600)
            delay = backoff + random.uniform(0, 30)
            logger.warning(
                f"[{bookmaker}] {consecutive_errors} consecutive errors — "
                f"backing off {delay:.0f}s"
            )
        else:
            # Normal randomized interval
            delay = random.uniform(POLL_MIN_INTERVAL, POLL_MAX_INTERVAL)

        _poller_stats[bookmaker]["next_delay"] = round(delay, 1)
        logger.debug(f"[{bookmaker}] Next poll in {delay:.1f}s")

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(f"[{bookmaker}] Auto-poller cancelled during sleep")
            return


# ─────────────────────────────────────────────────────────────────────────────
# Prediction Market Polling Loop
# ─────────────────────────────────────────────────────────────────────────────

PRED_POLL_INTERVAL = int(os.getenv("PRED_POLL_INTERVAL", "120"))  # 2 minutes

async def _prediction_market_poll_loop(market_name: str, adapter) -> None:
    """
    Continuous background polling loop for a prediction market.

    Prediction markets change slower than live sports, so we poll less
    frequently (default 2 min vs 45-90s for sportsbooks).
    """
    _poller_stats[market_name] = {
        "started_at": datetime.utcnow().isoformat(),
        "poll_count": 0,
        "success_count": 0,
        "error_count": 0,
        "last_poll_at": None,
        "last_error": None,
        "next_delay": None,
    }

    # Stagger start
    stagger = random.uniform(5, 30)
    logger.info(f"[{market_name}] Prediction market poller starting in {stagger:.1f}s")
    await asyncio.sleep(stagger)

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10
    BASE_ERROR_BACKOFF = 60

    while True:
        poll_start = datetime.utcnow()
        cycle_started_at = time.monotonic()
        _poller_stats[market_name]["poll_count"] += 1
        _poller_stats[market_name]["last_poll_at"] = poll_start.isoformat()

        try:
            odds = await adapter.fetch_markets()
            payload = [o.model_dump(mode="json") for o in odds] if odds else []
            pushed = False

            if payload:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{ODDS_INGEST_URL}/process",
                        json=payload,
                    )
                    resp.raise_for_status()
                pushed = True
                logger.info(f"[{market_name}] Fetched {len(odds)} odds → pushed to pipeline")
            else:
                logger.info(f"[{market_name}] No odds available this cycle")

            _poller_stats[market_name]["success_count"] += 1
            consecutive_errors = 0
            _record_prediction_market_cycle(
                market_name,
                status="ok",
                source_counts={market_name: len(odds)},
                batch_count=len(odds),
                pushed=pushed,
                duration_seconds=time.monotonic() - cycle_started_at,
                adapter_states={market_name: _prediction_market_adapter_state(adapter) or {}},
                payload=payload,
            )

        except asyncio.CancelledError:
            logger.info(f"[{market_name}] Poller cancelled")
            try:
                await adapter.close()
            except Exception:
                pass
            return
        except Exception as exc:
            consecutive_errors += 1
            _poller_stats[market_name]["error_count"] += 1
            _poller_stats[market_name]["last_error"] = str(exc)[:200]
            _record_prediction_market_cycle(
                market_name,
                status="error",
                source_counts={},
                batch_count=0,
                pushed=False,
                duration_seconds=time.monotonic() - cycle_started_at,
                adapter_states={market_name: _prediction_market_adapter_state(adapter) or {}},
                error=str(exc),
            )
            logger.error(f"[{market_name}] Poll error ({consecutive_errors}): {exc}")

        # Determine next sleep
        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            backoff = min(BASE_ERROR_BACKOFF * (2 ** (consecutive_errors - MAX_CONSECUTIVE_ERRORS)), 600)
            delay = backoff + random.uniform(0, 30)
            logger.warning(f"[{market_name}] {consecutive_errors} errors — backing off {delay:.0f}s")
        else:
            delay = PRED_POLL_INTERVAL + random.uniform(-30, 30)

        _poller_stats[market_name]["next_delay"] = round(delay, 1)

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(f"[{market_name}] Poller cancelled during sleep")
            try:
                await adapter.close()
            except Exception:
                pass
            return


async def _prediction_market_combined_poll_loop(poly_adapter: PolymarketAdapter, kalshi_adapter: KalshiAdapter) -> None:
    """Poll Polymarket + Kalshi together, unify IDs, and push a single batch.

    This is required for cross-market arbitrage detection because odds_ingest/arb_math
    only sees arbitrage opportunities within a single POSTed batch.
    """
    market_name = "prediction_markets"
    _poller_stats[market_name] = {
        "started_at": datetime.utcnow().isoformat(),
        "poll_count": 0,
        "success_count": 0,
        "error_count": 0,
        "last_poll_at": None,
        "last_error": None,
        "next_delay": None,
    }

    unifier = PredictionMarketEventUnifier()

    stagger = random.uniform(5, 30)
    logger.info(f"[{market_name}] Combined prediction poller starting in {stagger:.1f}s")
    await asyncio.sleep(stagger)

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10
    BASE_ERROR_BACKOFF = 60

    while True:
        poll_start = datetime.utcnow()
        cycle_started_at = time.monotonic()
        _poller_stats[market_name]["poll_count"] += 1
        _poller_stats[market_name]["last_poll_at"] = poll_start.isoformat()

        try:
            poly_res, kalshi_res = await asyncio.gather(
                poly_adapter.fetch_markets(),
                kalshi_adapter.fetch_markets(),
                return_exceptions=True,
            )

            poly_odds: List[MarketOdds] = []
            kalshi_odds: List[MarketOdds] = []
            fetch_errors: List[str] = []
            if isinstance(poly_res, Exception):
                logger.error(f"[{market_name}] Polymarket fetch error: {poly_res}")
                fetch_errors.append(f"polymarket: {poly_res}")
            else:
                poly_odds = poly_res or []

            if isinstance(kalshi_res, Exception):
                logger.error(f"[{market_name}] Kalshi fetch error: {kalshi_res}")
                fetch_errors.append(f"kalshi: {kalshi_res}")
            else:
                kalshi_odds = kalshi_res or []

            meta: Dict[str, Any] = {}
            if poly_odds and kalshi_odds:
                combined_odds, meta = unifier.unify(poly_odds, kalshi_odds)
                logger.info(
                    "[%s] poly=%s kalshi=%s combined=%s matched_pairs=%s",
                    market_name,
                    len(poly_odds),
                    len(kalshi_odds),
                    len(combined_odds),
                    meta.get("matched_pairs"),
                )
            else:
                combined_odds = poly_odds + kalshi_odds
                logger.info(
                    "[%s] poly=%s kalshi=%s combined=%s (no unify)",
                    market_name,
                    len(poly_odds),
                    len(kalshi_odds),
                    len(combined_odds),
                )

            payload = [o.model_dump(mode="json") for o in combined_odds] if combined_odds else []
            pushed = False
            if combined_odds:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(
                        f"{ODDS_INGEST_URL}/process",
                        json=payload,
                    )
                    resp.raise_for_status()
                pushed = True
                logger.info(f"[{market_name}] Pushed {len(combined_odds)} odds → pipeline")
            else:
                logger.info(f"[{market_name}] No odds available this cycle")

            _poller_stats[market_name]["success_count"] += 1
            consecutive_errors = 0
            _record_prediction_market_cycle(
                market_name,
                status="partial_error" if fetch_errors else "ok",
                source_counts={"polymarket": len(poly_odds), "kalshi": len(kalshi_odds)},
                batch_count=len(combined_odds),
                pushed=pushed,
                duration_seconds=time.monotonic() - cycle_started_at,
                adapter_states={
                    "polymarket": _prediction_market_adapter_state(poly_adapter) or {},
                    "kalshi": _prediction_market_adapter_state(kalshi_adapter) or {},
                },
                matching={
                    "matched_pairs": meta.get("matched_pairs"),
                    "best_candidate_score": meta.get("best_candidate_score"),
                },
                fetch_errors=fetch_errors,
                payload=payload,
            )

        except asyncio.CancelledError:
            logger.info(f"[{market_name}] Poller cancelled")
            try:
                await poly_adapter.close()
            except Exception:
                pass
            try:
                await kalshi_adapter.close()
            except Exception:
                pass
            return
        except Exception as exc:
            consecutive_errors += 1
            _poller_stats[market_name]["error_count"] += 1
            _poller_stats[market_name]["last_error"] = str(exc)[:200]
            _record_prediction_market_cycle(
                market_name,
                status="error",
                source_counts={},
                batch_count=0,
                pushed=False,
                duration_seconds=time.monotonic() - cycle_started_at,
                adapter_states={
                    "polymarket": _prediction_market_adapter_state(poly_adapter) or {},
                    "kalshi": _prediction_market_adapter_state(kalshi_adapter) or {},
                },
                error=str(exc),
            )
            logger.error(f"[{market_name}] Poll error ({consecutive_errors}): {exc}")

        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
            backoff = min(BASE_ERROR_BACKOFF * (2 ** (consecutive_errors - MAX_CONSECUTIVE_ERRORS)), 600)
            delay = backoff + random.uniform(0, 30)
            logger.warning(f"[{market_name}] {consecutive_errors} errors — backing off {delay:.0f}s")
        else:
            delay = PRED_POLL_INTERVAL + random.uniform(-30, 30)

        _poller_stats[market_name]["next_delay"] = round(delay, 1)

        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info(f"[{market_name}] Poller cancelled during sleep")
            try:
                await poly_adapter.close()
            except Exception:
                pass
            try:
                await kalshi_adapter.close()
            except Exception:
                pass
            return


@app.on_event("startup")
async def startup_event():
    """Initialize feeds and start auto-polling loops on startup."""
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

    # Optionally disable sportsbook auto-pollers when ODDS_API_KEY is missing/invalid.
    # This keeps logs clean and prevents repeated upstream 401 spam while allowing
    # prediction-market pollers to continue running.
    disable_on_invalid_env = os.getenv(
        "DISABLE_SPORTSBOOK_POLLERS_ON_ODDS_API_INVALID",
        "true",
    ).strip().lower()
    disable_on_invalid = disable_on_invalid_env in {"true", "1", "yes", "y"}
    use_odds_api_env = os.getenv("USE_ODDS_API", "true").strip().lower()
    use_odds_api = use_odds_api_env in {"true", "1", "yes", "y"}

    if disable_on_invalid and use_odds_api:
        odds_api_key = (os.getenv("ODDS_API_KEY") or "").strip()
        odds_api_books = [
            bm for bm in session_manager._configs.keys()
            if bm.lower() in {"fanduel", "draftkings", "fanatics"}
        ]

        if odds_api_books:
            if not odds_api_key:
                for bm in odds_api_books:
                    _disabled_pollers[bm] = "odds_api_key_missing"
                logger.warning(
                    "[sportsbooks] ODDS_API_KEY missing; auto-pollers disabled for: "
                    + ", ".join(odds_api_books)
                )
            else:
                is_valid = await _validate_odds_api_key(odds_api_key)
                if not is_valid:
                    for bm in odds_api_books:
                        _disabled_pollers[bm] = "invalid_odds_api_key"
                    logger.warning(
                        "[sportsbooks] ODDS_API_KEY appears invalid (401/403); "
                        "auto-pollers disabled for: " + ", ".join(odds_api_books)
                    )

    # Launch auto-pollers for every enabled bookmaker
    for bookmaker, config in session_manager._configs.items():
        if not config.enabled:
            logger.info(f"[{bookmaker}] Feed disabled — skipping auto-poller")
            continue

        if bookmaker in _disabled_pollers:
            logger.warning(
                f"[{bookmaker}] Auto-poller disabled ({_disabled_pollers[bookmaker]})"
            )
            continue

        task = asyncio.create_task(
            _auto_poll_loop(bookmaker),
            name=f"auto_poll_{bookmaker}",
        )
        _poller_tasks[bookmaker] = task
        logger.info(
            f"[{bookmaker}] Auto-poller scheduled "
            f"(interval {POLL_MIN_INTERVAL}-{POLL_MAX_INTERVAL}s randomized)"
        )

    # ── Prediction Market Pollers ──────────────────────────────────────────
    poly_enabled = os.getenv("POLYMARKET_ENABLED", "false").lower() == "true"
    kalshi_enabled = os.getenv("KALSHI_ENABLED", "false").lower() == "true"

    if poly_enabled and kalshi_enabled:
        poly_adapter = PolymarketAdapter()
        kalshi_adapter = KalshiAdapter()
        _prediction_market_adapters.extend([poly_adapter, kalshi_adapter])
        task = asyncio.create_task(
            _prediction_market_combined_poll_loop(poly_adapter, kalshi_adapter),
            name="auto_poll_prediction_markets",
        )
        _poller_tasks["prediction_markets"] = task
        logger.info("[prediction_markets] Auto-poller scheduled (combined Polymarket+Kalshi, ~2min interval)")
    else:
        if poly_enabled:
            poly_adapter = PolymarketAdapter()
            _prediction_market_adapters.append(poly_adapter)
            task = asyncio.create_task(
                _prediction_market_poll_loop("polymarket", poly_adapter),
                name="auto_poll_polymarket",
            )
            _poller_tasks["polymarket"] = task
            logger.info("[polymarket] Auto-poller scheduled (prediction market, ~2min interval)")

        if kalshi_enabled:
            kalshi_adapter = KalshiAdapter()
            _prediction_market_adapters.append(kalshi_adapter)
            task = asyncio.create_task(
                _prediction_market_poll_loop("kalshi", kalshi_adapter),
                name="auto_poll_kalshi",
            )
            _poller_tasks["kalshi"] = task
            logger.info("[kalshi] Auto-poller scheduled (prediction market, ~2min interval)")

    if _poller_tasks:
        logger.info(
            f"🚀 {len(_poller_tasks)} auto-pollers launched — "
            f"arb detection is fully automated"
        )


@app.on_event("shutdown")
async def shutdown_event():
    """Cancel pollers and clean up on shutdown."""
    logger.info("Market Feed service shutting down...")

    # Cancel all background pollers
    for bookmaker, task in _poller_tasks.items():
        if not task.done():
            task.cancel()
            logger.info(f"[{bookmaker}] Auto-poller cancelled")
    _poller_tasks.clear()

    session_manager.close_all()

    # Close prediction market adapters (HTTP clients)
    for adapter in list(_prediction_market_adapters):
        try:
            await adapter.close()
        except Exception:
            pass
    _prediction_market_adapters.clear()


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


# ─────────────────────────────────────────────────────────────────────────────
# Polling Status & Control
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/polling/status")
def polling_status() -> Dict:
    """Get status of all auto-polling background loops."""
    pollers = {}
    for bookmaker, task in _poller_tasks.items():
        stats = _poller_stats.get(bookmaker, {})
        pollers[bookmaker] = {
            "running": not task.done(),
            "interval_range": f"{POLL_MIN_INTERVAL}-{POLL_MAX_INTERVAL}s (randomized)",
            **stats,
        }
    return {
        "auto_polling_enabled": bool(_poller_tasks),
        "active_pollers": sum(1 for t in _poller_tasks.values() if not t.done()),
        "total_pollers": len(_poller_tasks),
        "pollers": pollers,
        "prediction_market_runtime": _prediction_market_runtime_store.snapshot_status(),
    }


@app.get("/prediction-markets/status")
def prediction_market_status() -> Dict[str, Any]:
    return {
        "enabled": _prediction_market_enabled_config(),
        "pollers": _prediction_market_pollers_status(),
        "adapters": _prediction_market_adapter_states(),
        "runtime": _prediction_market_runtime_store.snapshot_status(),
    }


@app.get("/prediction-markets/history")
def prediction_market_history(limit: int = 10) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    return {
        "limit": limit,
        "available": _prediction_market_runtime_store.snapshot_status()["history_size"],
        "history": _prediction_market_runtime_store.history(limit=limit),
    }


@app.post("/polling/stop/{bookmaker}")
def stop_poller(bookmaker: str) -> Dict:
    """Stop the auto-poller for a specific bookmaker."""
    task = _poller_tasks.get(bookmaker)
    if not task:
        raise HTTPException(status_code=404, detail=f"No poller for {bookmaker}")
    if task.done():
        return {"bookmaker": bookmaker, "message": "Poller already stopped"}
    task.cancel()
    return {"bookmaker": bookmaker, "message": "Poller stop requested"}


@app.post("/polling/start/{bookmaker}")
def start_poller(bookmaker: str) -> Dict:
    """Start (or restart) the auto-poller for a specific bookmaker."""
    if bookmaker not in session_manager._configs:
        raise HTTPException(status_code=404, detail=f"Feed not found: {bookmaker}")

    if bookmaker in _disabled_pollers:
        raise HTTPException(
            status_code=400,
            detail=f"Auto-poller disabled for {bookmaker} ({_disabled_pollers[bookmaker]})",
        )

    # Cancel existing if any
    existing = _poller_tasks.get(bookmaker)
    if existing and not existing.done():
        existing.cancel()
    task = asyncio.create_task(
        _auto_poll_loop(bookmaker),
        name=f"auto_poll_{bookmaker}",
    )
    _poller_tasks[bookmaker] = task
    return {"bookmaker": bookmaker, "message": "Poller started"}


# ── Prediction-Market Diagnostics ──────────────────────────────────────────


@app.get("/diagnostics/overlap")
async def diagnostics_overlap(
    poly_limit: int = 500,
    kalshi_limit: int = 1000,
    top_k: int = 25,
    min_score: float = 0.0,
) -> Dict:
    """
    On-demand overlap report between Polymarket and Kalshi.

    Fetches fresh data from both APIs, runs the fuzzy matcher with a *very*
    low threshold, and returns the top candidate pairs, similarity scores,
    and a keyword/topic breakdown per platform.

    Query parameters:
      - poly_limit  : Polymarket page size (default 500)
      - kalshi_limit: Kalshi page size (default 1000)
      - top_k       : Number of top candidate pairs to return (default 25)
      - min_score   : Minimum similarity score to include (default 0.0 — all)
    """
    import time as _time

    t0 = _time.monotonic()

    # Use temporary adapters so we don't interfere with the running poll loop
    poly = PolymarketAdapter()
    kalshi = KalshiAdapter()
    try:
        poly_odds_res, kalshi_odds_res = await asyncio.gather(
            poly.fetch_markets(),
            kalshi.fetch_markets(),
            return_exceptions=True,
        )
    finally:
        await poly.close()
        await kalshi.close()

    errors = []
    poly_odds: List[MarketOdds] = []
    kalshi_odds: List[MarketOdds] = []
    if isinstance(poly_odds_res, Exception):
        errors.append(f"Polymarket fetch error: {poly_odds_res}")
    else:
        poly_odds = poly_odds_res or []
    if isinstance(kalshi_odds_res, Exception):
        errors.append(f"Kalshi fetch error: {kalshi_odds_res}")
    else:
        kalshi_odds = kalshi_odds_res or []

    # Run unifier with very low threshold + debug on to capture best candidates
    saved_debug = os.environ.get("PM_MATCH_DEBUG")
    saved_score = os.environ.get("PM_MATCH_MIN_SCORE")
    saved_topk = os.environ.get("PM_MATCH_DEBUG_TOPK")
    os.environ["PM_MATCH_DEBUG"] = "true"
    os.environ["PM_MATCH_MIN_SCORE"] = str(min_score or 0.01)
    os.environ["PM_MATCH_DEBUG_TOPK"] = str(max(top_k, 50))
    try:
        diag_unifier = PredictionMarketEventUnifier()
        unified, meta = diag_unifier.unify(poly_odds, kalshi_odds)
    finally:
        # Restore env vars
        for k, v in [("PM_MATCH_DEBUG", saved_debug), ("PM_MATCH_MIN_SCORE", saved_score),
                     ("PM_MATCH_DEBUG_TOPK", saved_topk)]:
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # Build top candidate pairs from debug state
    top_pairs = []
    for score, pm_title, km_title in (diag_unifier._last_debug_top or [])[:top_k]:
        top_pairs.append({"score": round(score, 4), "polymarket": pm_title[:200], "kalshi": km_title[:200]})
    if diag_unifier._last_best_candidate and not top_pairs:
        bs, pm, km = diag_unifier._last_best_candidate
        top_pairs.append({"score": round(bs, 4), "polymarket": pm[:200], "kalshi": km[:200]})

    # Unique market titles per platform
    poly_titles = list({o.market for o in poly_odds})
    kalshi_titles = list({o.market for o in kalshi_odds})

    elapsed = round(_time.monotonic() - t0, 2)

    return {
        "elapsed_seconds": elapsed,
        "errors": errors,
        "counts": {
            "polymarket_odds": len(poly_odds),
            "kalshi_odds": len(kalshi_odds),
            "polymarket_unique_markets": len(poly_titles),
            "kalshi_unique_markets": len(kalshi_titles),
        },
        "matching": {
            "matched_pairs": meta.get("matched_pairs", 0),
            "best_candidate_score": meta.get("best_candidate_score"),
            "match_threshold": diag_unifier.min_match_score,
        },
        "top_candidate_pairs": top_pairs,
        "topic_breakdown": {
            "polymarket": categorize_prediction_market_titles(poly_titles),
            "kalshi": categorize_prediction_market_titles(kalshi_titles),
        },
        "sample_markets": {
            "polymarket": [t[:150] for t in poly_titles[:10]],
            "kalshi": [t[:150] for t in kalshi_titles[:10]],
        },
    }


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
# The Odds API Integration (Pre-game odds, no browser needed)
# ─────────────────────────────────────────────────────────────────────────────

from .adapters.odds_api_adapter import OddsAPIAdapter

# Singleton adapter instance
_odds_api_adapter: Optional[OddsAPIAdapter] = None


def _get_odds_api_adapter() -> OddsAPIAdapter:
    """Get or create the Odds API adapter singleton."""
    global _odds_api_adapter
    if _odds_api_adapter is None:
        _odds_api_adapter = OddsAPIAdapter()
    return _odds_api_adapter


@app.get("/odds-api/odds")
async def get_odds_api_odds(
    sports: Optional[str] = None,
    bookmakers: Optional[str] = None,
    markets: Optional[str] = None,
) -> ScrapeResult:
    """
    Fetch pre-game odds from The Odds API.

    This is the recommended way to get pre-game odds - no login required,
    no browser automation, no anti-bot issues.

    Args:
        sports: Comma-separated sports (e.g., "nfl,nba"). Default: all major sports.
        bookmakers: Comma-separated bookmakers. Default: fanduel,draftkings,fanatics.
        markets: Comma-separated markets. Default: h2h,spreads,totals.

    Returns:
        ScrapeResult with normalized MarketOdds from all requested bookmakers.
    """
    adapter = _get_odds_api_adapter()

    sport_list = sports.split(",") if sports else None
    book_list = bookmakers.split(",") if bookmakers else None
    market_list = markets.split(",") if markets else None

    return adapter.get_odds(
        sports=sport_list,
        bookmakers=book_list,
        markets=market_list,
    )


@app.get("/odds-api/live")
async def get_odds_api_live(
    sports: Optional[str] = None,
    bookmakers: Optional[str] = None,
) -> ScrapeResult:
    """
    Fetch live/in-play odds from The Odds API.

    NOTE: Live odds from The Odds API have 5-30 second delay.
    For true real-time live odds, use the intercepting adapter (requires login).

    Args:
        sports: Comma-separated sports. Default: all major sports.
        bookmakers: Comma-separated bookmakers. Default: CT legal books.
    """
    adapter = _get_odds_api_adapter()

    sport_list = sports.split(",") if sports else None
    book_list = bookmakers.split(",") if bookmakers else None

    return adapter.get_live_odds(
        sports=sport_list,
        bookmakers=book_list,
    )


@app.get("/odds-api/status")
async def odds_api_status() -> Dict:
    """Check Odds API status and rate limit info."""
    adapter = _get_odds_api_adapter()

    api_key_configured = bool(adapter.api_key)

    return {
        "configured": api_key_configured,
        "requests_remaining": adapter.requests_remaining,
        "message": "ODDS_API_KEY is set" if api_key_configured else "ODDS_API_KEY not configured - set in .env",
    }


@app.post("/odds-api/fetch-and-push")
async def odds_api_fetch_and_push(
    sports: Optional[str] = None,
    bookmakers: Optional[str] = None,
) -> Dict:
    """
    Fetch odds from The Odds API and push to odds_ingest for arb detection.

    This is the main endpoint for automated pre-game arb detection using
    the third-party API (no browser scraping).
    """
    adapter = _get_odds_api_adapter()

    sport_list = sports.split(",") if sports else None
    book_list = bookmakers.split(",") if bookmakers else None

    result = adapter.get_odds(sports=sport_list, bookmakers=book_list)

    if not result.success:
        return {"success": False, "error": result.error}

    if not result.odds:
        return {"success": True, "message": "No odds available", "count": 0}

    # Push to odds_ingest
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ODDS_INGEST_URL}/process",
                json=[odds.model_dump(mode="json") for odds in result.odds],
            )
            response.raise_for_status()

        logger.info(f"[odds_api] Pushed {len(result.odds)} odds to pipeline")

        return {
            "success": True,
            "odds_count": len(result.odds),
            "requests_remaining": adapter.requests_remaining,
            "message": f"Fetched and pushed {len(result.odds)} odds from The Odds API",
        }
    except Exception as e:
        logger.error(f"[odds_api] Failed to push to odds_ingest: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Live Odds via API Interception (Real-time, requires login)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/live/status")
async def live_interception_status() -> Dict:
    """
    Check which bookmakers are ready for live odds interception.

    Returns login status for each CT sportsbook. Live interception requires
    an active login session (via browser automation or imported cookies).
    """
    ct_books = ["fanduel", "draftkings", "fanatics"]
    status = {}

    for bookmaker in ct_books:
        adapter = session_manager.get_adapter(bookmaker)
        if adapter:
            status[bookmaker] = {
                "configured": True,
                "logged_in": adapter.session_status.logged_in,
                "session_valid": adapter.session_status.session_valid,
                "adapter_type": type(adapter).__name__,
            }
        else:
            config_exists = bookmaker in session_manager._configs
            status[bookmaker] = {
                "configured": config_exists,
                "logged_in": False,
                "session_valid": False,
                "adapter_type": None,
            }

    return {
        "ready": any(s.get("session_valid") for s in status.values()),
        "bookmakers": status,
        "message": "Use POST /live/scrape/{bookmaker} to fetch live odds via API interception",
    }


@app.post("/live/scrape/{bookmaker}")
async def scrape_live_odds(bookmaker: str) -> ScrapeResult:
    """
    Scrape live odds from a bookmaker using API interception.

    This uses Playwright to open the sportsbook's live page and intercepts
    the network requests to capture odds data directly from their internal API.

    **Requires:**
    - Bookmaker credentials configured
    - Successful login (may require 2FA via Slack)

    This is the recommended approach for real-time live odds - no delay
    unlike third-party APIs.
    """
    if bookmaker.lower() not in ["fanduel", "draftkings", "fanatics"]:
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error=f"Live interception only supported for CT books: fanduel, draftkings, fanatics",
        )

    if bookmaker not in session_manager._configs:
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error=f"Bookmaker {bookmaker} not configured. Add to FEED_CONFIGS.",
        )

    # Ensure logged in (may trigger 2FA flow)
    if not session_manager.ensure_logged_in(bookmaker):
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error="Failed to login. Check credentials or use 2FA via Slack.",
        )

    # Get adapter and scrape
    adapter = session_manager.get_adapter(bookmaker)
    if not adapter:
        return ScrapeResult(
            bookmaker=bookmaker,
            success=False,
            error="Adapter not available",
        )

    return adapter.scrape()


@app.post("/live/scrape-and-push/{bookmaker}")
async def scrape_live_and_push(bookmaker: str) -> Dict:
    """
    Scrape live odds via API interception and push to arb detection pipeline.

    This is the main endpoint for automated live arb detection using
    browser-based scraping with network interception.
    """
    result = await scrape_live_odds(bookmaker)

    if not result.success:
        return {"success": False, "error": result.error}

    if not result.odds:
        return {"success": True, "message": "No live odds available", "count": 0}

    # Push to odds_ingest
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ODDS_INGEST_URL}/process",
                json=[odds.model_dump(mode="json") for odds in result.odds],
            )
            response.raise_for_status()

        logger.info(f"[{bookmaker}] Pushed {len(result.odds)} live odds to pipeline")

        return {
            "success": True,
            "odds_count": len(result.odds),
            "live_odds_count": sum(1 for o in result.odds if o.is_live),
            "message": f"Fetched and pushed {len(result.odds)} live odds",
        }
    except Exception as e:
        logger.error(f"[{bookmaker}] Failed to push live odds: {e}")
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Visual Browser Login (User logs in manually in visible browser window)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/login/visual/{bookmaker}")
async def visual_login(bookmaker: str, timeout_seconds: int = 300) -> Dict:
    """
    Open a visible browser window for manual login.

    A Chrome window pops up to the sportsbook login page. You log in
    manually (including 2FA), and the system saves your session. After
    successful login, the browser closes and future scraping uses your
    saved session.

    **Steps:**
    1. Call this endpoint
    2. Browser window opens to login page
    3. Log in manually (complete 2FA if required)
    4. System detects login success, saves session, closes browser
    5. All future scraping uses saved session until expiration

    Args:
        bookmaker: The sportsbook (fanduel, draftkings, fanatics)
        timeout_seconds: How long to wait for login (default 5 minutes)

    Returns:
        Success status and session info
    """
    from .adapters.ct_sportsbooks import get_ct_config
    from .stealth_playwright import StealthBrowser

    bookmaker_lower = bookmaker.lower()

    if bookmaker_lower not in ["fanduel", "draftkings", "fanatics"]:
        return {
            "success": False,
            "error": f"Visual login only supported for CT books: fanduel, draftkings, fanatics",
        }

    # Get config for login URL
    config = get_ct_config(bookmaker_lower)
    if not config:
        return {
            "success": False,
            "error": f"No configuration found for {bookmaker}",
        }

    login_url = config.get("login_url")
    if not login_url:
        return {
            "success": False,
            "error": f"No login_url configured for {bookmaker}",
        }

    logger.info(f"[{bookmaker}] Starting visual login flow")

    # Create a new StealthBrowser instance in non-headless mode
    browser = StealthBrowser(
        bookmaker=bookmaker_lower,
        headless=False,  # Visible browser window
        geo="US",
    )

    try:
        success = await browser.visual_login(
            login_url=login_url,
            timeout_seconds=timeout_seconds,
        )

        if success:
            logger.info(f"[{bookmaker}] Visual login successful, session saved")

            # Invalidate any existing adapter so it picks up new session
            if bookmaker_lower in session_manager._adapters:
                old_adapter = session_manager._adapters.pop(bookmaker_lower)
                try:
                    old_adapter.close()
                except Exception:
                    pass

            return {
                "success": True,
                "bookmaker": bookmaker_lower,
                "message": f"Login successful! Session saved for {bookmaker}. Future scraping will use this session.",
                "session_dir": str(browser.session_dir),
            }
        else:
            return {
                "success": False,
                "bookmaker": bookmaker_lower,
                "error": "Login not completed within timeout. Please try again.",
            }

    except Exception as e:
        logger.error(f"[{bookmaker}] Visual login failed: {e}")
        return {
            "success": False,
            "bookmaker": bookmaker_lower,
            "error": str(e),
        }


@app.get("/login/status")
async def login_status() -> Dict:
    """
    Check login/session status for all CT sportsbooks.

    Returns which bookmakers have saved sessions and whether they're valid.
    """
    from pathlib import Path

    ct_books = ["fanduel", "draftkings", "fanatics"]
    status = {}

    for bookmaker in ct_books:
        session_dir = Path(f"/tmp/sessions/{bookmaker}")
        session_file = session_dir / "session.json"

        has_session = session_file.exists()
        session_age = None

        if has_session:
            try:
                import os
                mtime = os.path.getmtime(session_file)
                from datetime import datetime
                session_age = datetime.utcnow().timestamp() - mtime
            except Exception:
                pass

        # Check adapter status
        adapter = session_manager._adapters.get(bookmaker)
        adapter_status = None
        if adapter:
            adapter_status = {
                "type": type(adapter).__name__,
                "logged_in": adapter.session_status.logged_in,
                "session_valid": adapter.session_status.session_valid,
            }

        status[bookmaker] = {
            "has_saved_session": has_session,
            "session_age_seconds": int(session_age) if session_age else None,
            "session_age_human": _format_duration(session_age) if session_age else None,
            "adapter": adapter_status,
        }

    return {
        "bookmakers": status,
        "message": "Use POST /login/visual/{bookmaker} to log in via visible browser window",
    }


def _format_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m {int(seconds % 60)}s"
    elif seconds < 86400:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        return f"{hours}h {mins}m"
    else:
        days = int(seconds / 86400)
        hours = int((seconds % 86400) / 3600)
        return f"{days}d {hours}h"


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


# ─────────────────────────────────────────────────────────────────────────────
# Cookie-Based Session Import
# ─────────────────────────────────────────────────────────────────────────────

# In-memory cookie store (persisted to disk)
COOKIE_DIR = Path(os.getenv("COOKIE_DIR", "/tmp/arb-desk-cookies"))
COOKIE_DIR.mkdir(parents=True, exist_ok=True)

_imported_cookies: Dict[str, List[Dict]] = {}


def _load_cookies_from_disk() -> None:
    """Load saved cookies from disk on startup."""
    global _imported_cookies
    for cookie_file in COOKIE_DIR.glob("*.json"):
        bookmaker = cookie_file.stem
        try:
            with open(cookie_file, "r") as f:
                _imported_cookies[bookmaker] = json.load(f)
            logger.info(f"[{bookmaker}] Loaded {len(_imported_cookies[bookmaker])} cookies from disk")
        except Exception as e:
            logger.warning(f"[{bookmaker}] Failed to load cookies: {e}")


def _save_cookies_to_disk(bookmaker: str, cookies: List[Dict]) -> None:
    """Save cookies to disk for persistence across restarts."""
    cookie_file = COOKIE_DIR / f"{bookmaker}.json"
    try:
        with open(cookie_file, "w") as f:
            json.dump(cookies, f)
        logger.info(f"[{bookmaker}] Saved {len(cookies)} cookies to disk")
    except Exception as e:
        logger.warning(f"[{bookmaker}] Failed to save cookies: {e}")


def get_imported_cookies(bookmaker: str) -> Optional[List[Dict]]:
    """Get imported cookies for a bookmaker."""
    return _imported_cookies.get(bookmaker.lower())


@app.post("/cookies/import/{bookmaker}")
async def import_cookies(bookmaker: str, cookies: List[Dict]) -> Dict:
    """
    Import browser cookies for a bookmaker.

    This allows you to log in manually in your browser, export cookies,
    and import them here so the scraper can use your authenticated session.

    Expected cookie format (from browser):
    [
        {"name": "cookie_name", "value": "cookie_value", "domain": ".fanduel.com", ...},
        ...
    ]
    """
    bookmaker = bookmaker.lower()

    if bookmaker not in session_manager._configs:
        raise HTTPException(status_code=404, detail=f"Unknown bookmaker: {bookmaker}")

    if not cookies:
        raise HTTPException(status_code=400, detail="No cookies provided")

    # Store cookies
    _imported_cookies[bookmaker] = cookies
    _save_cookies_to_disk(bookmaker, cookies)

    # Mark session as valid (we'll use cookies instead of login)
    adapter = session_manager.get_adapter(bookmaker)
    if adapter:
        adapter.session_status.logged_in = True
        adapter.session_status.session_valid = True
        adapter.session_status.last_login_at = datetime.utcnow()
        adapter.session_status.login_failures = 0
        adapter.session_status.error = None

    logger.info(f"[{bookmaker}] Imported {len(cookies)} cookies — session marked as valid")

    return {
        "success": True,
        "bookmaker": bookmaker,
        "cookies_imported": len(cookies),
        "message": f"Session cookies imported. {bookmaker} is now authenticated.",
    }


@app.get("/cookies/status")
async def cookies_status() -> Dict:
    """Check which bookmakers have imported cookies."""
    status = {}
    for bookmaker in session_manager._configs.keys():
        cookies = _imported_cookies.get(bookmaker, [])
        status[bookmaker] = {
            "has_cookies": len(cookies) > 0,
            "cookie_count": len(cookies),
            "cookie_file_exists": (COOKIE_DIR / f"{bookmaker}.json").exists(),
        }
    return status


@app.delete("/cookies/{bookmaker}")
async def clear_cookies(bookmaker: str) -> Dict:
    """Clear imported cookies for a bookmaker."""
    bookmaker = bookmaker.lower()

    _imported_cookies.pop(bookmaker, None)

    cookie_file = COOKIE_DIR / f"{bookmaker}.json"
    if cookie_file.exists():
        cookie_file.unlink()

    # Invalidate session
    adapter = session_manager.get_adapter(bookmaker)
    if adapter:
        adapter.session_status.logged_in = False
        adapter.session_status.session_valid = False

    return {"success": True, "bookmaker": bookmaker, "message": "Cookies cleared"}


# Load cookies on module import
_load_cookies_from_disk()
