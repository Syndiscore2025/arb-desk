"""
Prediction Market Adapters

API-based adapters for prediction markets:
- Polymarket (crypto-based, wide event coverage)
- Kalshi (CFTC-regulated, event contracts)

Cross-market arbitrage between prediction markets and sportsbooks.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import httpx

from shared.schemas import MarketOdds

# Kalshi RSA-PSS imports (optional — only needed if Kalshi is enabled)
try:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

logger = logging.getLogger(__name__)


class PolymarketAdapter:
    """
    Polymarket API adapter for prediction market odds.

    Polymarket uses CLOB (central limit order book) with prices as probabilities.
    Price of $0.65 = 65% implied probability = 1.538 decimal odds.
    """

    BASE_URL = "https://clob.polymarket.com"
    GAMMA_URL = "https://gamma-api.polymarket.com"

    # Map sportsbook events to Polymarket slugs/tags
    SPORT_TAGS = {
        "nfl": ["nfl", "football", "super-bowl"],
        "nba": ["nba", "basketball"],
        "mlb": ["mlb", "baseball", "world-series"],
        "nhl": ["nhl", "hockey", "stanley-cup"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.client = httpx.AsyncClient(timeout=30)
        self.page_limit = int(os.getenv("POLYMARKET_PAGE_LIMIT", "500"))
        self.max_pages = int(os.getenv("POLYMARKET_MAX_PAGES", "5"))
        # Rotate pagination across polls so we eventually scan deeper than the first N pages.
        # This improves the chance of finding cross-platform overlaps without any platform-specific mapping.
        self._rotate_pagination = str(os.getenv("POLYMARKET_ROTATE_PAGINATION", "true")).lower() in {
            "1", "true", "yes", "y", "on"
        }
        try:
            self._next_offset = max(0, int(os.getenv("POLYMARKET_START_OFFSET", "0")))
        except Exception:
            self._next_offset = 0

    async def fetch_markets(self, sport: Optional[str] = None) -> List[MarketOdds]:
        """Fetch all active markets, optionally filtered by sport."""
        odds_list: List[MarketOdds] = []

        try:
            # Fetch active markets from Gamma API (paginated)
            offset = self._next_offset if self._rotate_pagination else 0
            hit_end = False
            for page in range(self.max_pages):
                response = await self.client.get(
                    f"{self.GAMMA_URL}/markets",
                    params={
                        "active": "true",
                        "closed": "false",
                        "limit": self.page_limit,
                        "offset": offset,
                    },
                )
                response.raise_for_status()

                data = response.json()
                markets = data.get("markets", []) if isinstance(data, dict) else data
                if not isinstance(markets, list) or not markets:
                    hit_end = True
                    break

                for market in markets:
                    # Filter by sport if specified
                    if sport and not self._matches_sport(market, sport):
                        continue

                    # Convert to MarketOdds
                    market_odds = self._parse_market(market)
                    odds_list.extend(market_odds)

                offset += len(markets)
                if len(markets) < self.page_limit:
                    hit_end = True
                    break

            # Persist our position for the next poll so we cover more of the catalog over time.
            if self._rotate_pagination:
                self._next_offset = 0 if hit_end else offset

        except Exception as e:
            logger.error(f"[Polymarket] Failed to fetch markets: {e}")

        logger.info(f"[Polymarket] Fetched {len(odds_list)} market odds")
        return odds_list

    def _matches_sport(self, market: Dict, sport: str) -> bool:
        """Check if market matches sport category."""
        tags = self.SPORT_TAGS.get(sport.lower(), [])
        market_tags = market.get("tags", [])
        market_question = market.get("question", "").lower()

        for tag in tags:
            if tag in [t.lower() for t in market_tags]:
                return True
            if tag in market_question:
                return True
        return False

    def _parse_market(self, market: Dict) -> List[MarketOdds]:
        """Parse Polymarket market into MarketOdds."""
        odds_list = []

        condition_id = market.get("conditionId", market.get("id", "unknown"))
        question = market.get("question", "Unknown")

        # Get outcomes with prices — Gamma API returns these as JSON strings
        raw_outcomes = market.get("outcomes", [])
        raw_prices = market.get("outcomePrices", [])
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])

        # Normalize binary Yes/No markets so they align with Kalshi selections
        normalized = [self._normalize_yes_no(o) for o in outcomes] if isinstance(outcomes, list) else []
        is_binary_yesno = (
            isinstance(outcomes, list)
            and len(outcomes) == 2
            and all(normalized)
            and set(normalized) == {"Yes", "No"}
        )

        for i, outcome in enumerate(outcomes):
            if i >= len(prices):
                break

            try:
                price = float(prices[i])
                if price <= 0 or price >= 1:
                    continue

                # Convert probability to decimal odds
                decimal_odds = round(1 / price, 4)

                odds_list.append(MarketOdds(
                    event_id=f"poly-{condition_id}",
                    sport="prediction",
                    # Keep more context for cross-platform matching; still bounded.
                    market=question[:160],
                    bookmaker="polymarket",
                    selection=(normalized[i] if is_binary_yesno else str(outcome)),
                    odds_decimal=decimal_odds,
                    market_type="prediction",
                    expires_at=self._parse_end_date(market),
                ))
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse outcome {outcome}: {e}")

        return odds_list

    def _parse_end_date(self, market: Dict) -> Optional[datetime]:
        """Parse market end date."""
        end_str = market.get("endDateIso") or market.get("endDate")
        if end_str:
            try:
                return datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    @staticmethod
    def _normalize_yes_no(value: Any) -> Optional[str]:
        if value is None:
            return None
        s = str(value).strip().lower()
        if s in {"yes", "y", "true"}:
            return "Yes"
        if s in {"no", "n", "false"}:
            return "No"
        return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class KalshiAdapter:
    """
    Kalshi API adapter for CFTC-regulated event contracts.

    Kalshi prices are in cents (0-100), representing probability.
    Price of 65 cents = 65% probability = 1.538 decimal odds.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

    # Kalshi event categories that overlap with sports
    SPORTS_CATEGORIES = ["sports", "nfl", "nba", "mlb", "nhl"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.use_demo = self.config.get("use_demo", False)
        self.base_url = self.DEMO_URL if self.use_demo else self.BASE_URL
        self.client = httpx.AsyncClient(timeout=30)

        # For optional authenticated calls, Kalshi signatures need the path prefix
        # (e.g. "/trade-api/v2"). This is safe even when we only use public reads.
        try:
            self._path_prefix = httpx.URL(self.base_url).path
        except Exception:
            self._path_prefix = "/trade-api/v2"

        # Simple global rate limiting/backoff to reduce 429 storms when polling many series.
        # Keep this generic (no per-series hacks) and controllable via env vars.
        self._rl_lock = asyncio.Lock()
        self._cooldown_until: float = 0.0
        self._last_request_at: float = 0.0
        self._min_request_interval_s = float(os.getenv("KALSHI_MIN_REQUEST_INTERVAL_SECONDS", "0.20"))

        # Series discovery/cache (so we can cover all categories without hardcoding)
        self._series_cache: List[str] = []
        self._series_cache_fetched_at: float = 0.0
        self._series_rr_idx: int = 0

        # Global markets pagination cursor (rotates across polls so we eventually scan deeper).
        self._rotate_global_cursor = str(os.getenv("KALSHI_GLOBAL_ROTATE_CURSOR", "true")).lower() in {
            "1", "true", "yes", "y", "on"
        }
        self._global_markets_cursor: Optional[str] = None

        # Event-based market discovery cache (captures non-sports markets with
        # status=active that the global status=open query misses).
        self._event_markets_cache: List[MarketOdds] = []
        self._event_markets_fetched_at: float = 0.0

        # Load API key and private key for RSA-PSS authentication
        self.api_key = os.getenv("KALSHI_API_KEY")
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

        self.private_key = None
        if private_key_path and os.path.exists(private_key_path):
            if not _HAS_CRYPTO:
                logger.error("[Kalshi] cryptography package not installed — cannot authenticate")
            else:
                try:
                    with open(private_key_path, "rb") as key_file:
                        self.private_key = serialization.load_pem_private_key(
                            key_file.read(),
                            password=None,
                            backend=default_backend(),
                        )
                    logger.info("[Kalshi] RSA private key loaded successfully")
                except Exception as e:
                    logger.error(f"[Kalshi] Failed to load private key: {e}")
        else:
            if not private_key_path:
                logger.warning("[Kalshi] KALSHI_PRIVATE_KEY_PATH not set")
            elif not os.path.exists(private_key_path):
                logger.warning(f"[Kalshi] Key file not found: {private_key_path}")

    # ── HTTP Helpers (rate limit + 429 handling) ───────────────────────────

    def _retry_after_seconds(self, response: httpx.Response) -> Optional[float]:
        ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if not ra:
            return None
        try:
            return float(ra)
        except Exception:
            return None

    async def _throttle(self) -> None:
        """Enforce global cooldown + minimum spacing between request *starts*."""
        async with self._rl_lock:
            now = time.time()
            if now < self._cooldown_until:
                await asyncio.sleep(self._cooldown_until - now)

            # Ensure a minimum interval between request starts
            if self._min_request_interval_s > 0:
                since = time.time() - self._last_request_at
                if since < self._min_request_interval_s:
                    await asyncio.sleep(self._min_request_interval_s - since)
            self._last_request_at = time.time()

    async def _get_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET JSON from Kalshi with conservative retry/backoff on 429."""
        url = f"{self.base_url}{path}"
        retries = int(os.getenv("KALSHI_429_RETRIES", "2"))
        backoff_base = float(os.getenv("KALSHI_429_BACKOFF_SECONDS", "1.0"))
        max_sleep = float(os.getenv("KALSHI_429_MAX_SLEEP_SECONDS", "30"))

        # Include auth headers when available; reads work without them.
        sig_path = f"{self._path_prefix}{path}"
        headers = self._auth_headers("GET", sig_path)

        for attempt in range(max(0, retries) + 1):
            await self._throttle()
            resp = await self.client.get(url, params=params, headers=headers)
            if resp.status_code != 429:
                resp.raise_for_status()
                data = resp.json()
                return data or {}

            # 429: respect Retry-After if present, otherwise exponential backoff.
            wait_s = self._retry_after_seconds(resp)
            if wait_s is None:
                wait_s = backoff_base * (2 ** attempt)
            wait_s = max(0.0, min(float(wait_s), max_sleep))

            async with self._rl_lock:
                self._cooldown_until = max(self._cooldown_until, time.time() + wait_s)

            logger.warning(
                "[Kalshi] 429 rate limited on %s (attempt %s/%s) — sleeping %.2fs",
                path,
                attempt + 1,
                retries + 1,
                wait_s,
            )
            if wait_s:
                await asyncio.sleep(wait_s)

        # Give up: let callers degrade gracefully.
        return {}

    # ── RSA-PSS Signing ────────────────────────────────────────────────────

    def _sign_request(self, method: str, path: str) -> tuple:
        """
        Create RSA-PSS signature for a Kalshi API request.

        Kalshi auth headers:
          KALSHI-ACCESS-KEY       – API key id
          KALSHI-ACCESS-TIMESTAMP – current epoch ms
          KALSHI-ACCESS-SIGNATURE – base64(RSA-PSS(timestamp + METHOD + path_no_query))

        Returns:
            (signature_b64, timestamp_str)
        """
        if not self.private_key:
            raise RuntimeError("Kalshi private key not loaded — cannot sign request")

        timestamp = str(int(datetime.utcnow().timestamp() * 1000))
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")

        signature = self.private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8"), timestamp

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Return Kalshi auth headers dict (or empty if keys missing)."""
        if not self.api_key or not self.private_key:
            return {}
        try:
            sig, ts = self._sign_request(method, path)
            return {
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts,
            }
        except Exception as e:
            logger.error(f"[Kalshi] Failed to sign request: {e}")
            return {}

    # ── Market Fetching ────────────────────────────────────────────────────

    # All sport series tickers we care about
    SPORT_SERIES = {
        "basketball_nba": "KXNBA",
        "americanfootball_nfl": "KXNFL",
        "baseball_mlb": "KXMLB",
        "icehockey_nhl": "KXNHL",
    }

    async def fetch_markets(self, sport: Optional[str] = None) -> List[MarketOdds]:
        """Fetch open markets from Kalshi (public endpoint, no auth needed).

        Notes:
        - Kalshi's /markets endpoint supports `mve_filter=exclude` to omit
          multivariate event (parlay/combo) markets.
        - When sport is None (default), we prefer a global open-markets listing
          with `mve_filter=exclude` because it yields broad coverage with far
          fewer requests than polling many series tickers.
        - When sport matches a known sports series key, we keep the targeted
          series_ticker query.
        """
        odds_list: List[MarketOdds] = []

        # Targeted sports series fetch (kept for callers that explicitly want it)
        if sport and sport.lower() in self.SPORT_SERIES:
            odds_list = await self._fetch_markets_for_series(self.SPORT_SERIES[sport.lower()])
            logger.info(f"[Kalshi] Fetched {len(odds_list)} market odds (series)")
            return odds_list

        # Default: global open markets, excluding MVEs (parlays)
        odds_list = await self._fetch_open_markets_global()
        global_count = len(odds_list)

        # Also fetch non-sports markets via event discovery (politics,
        # entertainment, world events, economics, science, climate).
        # These have status=active and are invisible to the status=open query.
        try:
            event_odds = await self._fetch_event_based_markets()
            if event_odds:
                # Merge & deduplicate: event-based markets take priority when
                # the same ticker appears in both sets (unlikely but safe).
                existing_ids = {o.event_id for o in odds_list}
                new_event = [o for o in event_odds if o.event_id not in existing_ids]
                odds_list.extend(new_event)
                logger.info(
                    f"[Kalshi] Fetched {len(odds_list)} market odds "
                    f"({global_count} global open + {len(new_event)} event-based)"
                )
                return odds_list
        except Exception as e:
            logger.warning(f"[Kalshi] Event-based market fetch failed (non-fatal): {e}")

        if odds_list:
            logger.info(f"[Kalshi] Fetched {len(odds_list)} market odds (global open, mve_filter=exclude)")
            return odds_list

        # Fallback: poll a rotating slice of discovered series tickers (best-effort)
        series_tickers = await self._get_series_tickers_for_poll()
        if not series_tickers:
            logger.warning("[Kalshi] No series tickers available for polling")
            return []

        # Fetch markets for selected series concurrently (bounded)
        concurrency = int(os.getenv("KALSHI_SERIES_CONCURRENCY", "3"))
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _fetch_one(ticker: str) -> List[MarketOdds]:
            async with sem:
                try:
                    return await self._fetch_markets_for_series(ticker)
                except Exception as e:
                    logger.error(f"[Kalshi] Failed to fetch {ticker}: {e}")
                    return []

        tasks = [asyncio.create_task(_fetch_one(t)) for t in series_tickers]
        for res in await asyncio.gather(*tasks):
            odds_list.extend(res)

        logger.info(f"[Kalshi] Fetched {len(odds_list)} market odds (series fallback)")
        return odds_list

    async def _fetch_open_markets_global(self) -> List[MarketOdds]:
        """Fetch open non-MVE markets via the global /markets listing (paginated)."""
        odds_list: List[MarketOdds] = []
        cursor: Optional[str] = self._global_markets_cursor if self._rotate_global_cursor else None

        limit = int(os.getenv("KALSHI_GLOBAL_MARKETS_PAGE_LIMIT", "1000"))
        limit = max(1, min(limit, 1000))
        max_pages = int(os.getenv("KALSHI_GLOBAL_MAX_PAGES", "2"))
        max_pages = max(1, max_pages)

        page_count = 0
        hit_end = False
        while True:
            page_count += 1
            params: Dict[str, Any] = {
                "status": "open",
                "limit": limit,
                "mve_filter": "exclude",
            }
            if cursor:
                params["cursor"] = cursor

            data = await self._get_json("/markets", params=params)
            if not data:
                hit_end = True
                break

            markets = data.get("markets", [])
            if not isinstance(markets, list) or not markets:
                hit_end = True
                break

            for market in markets:
                if not isinstance(market, dict):
                    continue
                # Extra safety: skip MVE tickers even though mve_filter=exclude.
                if str(market.get("ticker", "")).startswith("KXMVE"):
                    continue
                # We only emit Yes/No prices; skip non-binary markets to avoid
                # misinterpreting scalar markets.
                if str(market.get("market_type", "")).lower() not in {"binary", ""}:
                    continue
                odds_list.extend(self._parse_market(market))

            cursor = data.get("cursor") or data.get("next_cursor") or data.get("next")
            if page_count >= max_pages:
                break
            if not cursor or len(markets) < limit:
                hit_end = True
                break

        # Persist cursor across polls so we cover more than the first pages over time.
        if self._rotate_global_cursor:
            self._global_markets_cursor = None if hit_end else cursor

        return odds_list

    # Event ticker prefixes to skip in event-based discovery.
    # Sports: already covered by the ``status=open`` global query.
    # Elections: state-by-state political races with hundreds of events
    #   (mostly ``status=initialized``), not useful for cross-market arb.
    _SKIP_EVENT_PREFIXES = (
        # Sports / esports
        "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXMMA", "KXSOCCER",
        "KXNCAAB", "KXNCAAF", "KXPGA", "KXTENNIS", "KXWNBA", "KXMLS",
        "KXESPORT", "KXLOL", "KXCSGO", "KXDOTA", "KXVALORANT", "KXCOD",
        # State-by-state political races (SENATEAL-28, GOVERNORCA-28, etc.)
        "SENATE", "GOVERNOR", "GOVPARTY", "HOUSE", "AGPARTY",
        "KXSENATE", "KXGOVERNOR", "KXGOVPARTY", "KXHOUSE", "KXAGPARTY",
    )

    # Hard cap on events to query per cycle (even after prefix filtering).
    _MAX_EVENTS_PER_CYCLE = 100

    async def _fetch_event_based_markets(self) -> List[MarketOdds]:
        """Fetch non-sports markets via event discovery.

        Kalshi's ``/markets?status=open`` returns only sports/esports.
        Politics, entertainment, world-events, economics, science and climate
        markets have ``status=active`` and are invisible to that query.

        Strategy:
        1. ``GET /events`` → discover event tickers (1–2 API calls).
        2. Filter out sports event prefixes (already covered by status=open).
        3. For each remaining event, ``GET /markets?event_ticker=X`` and keep
           only ``active`` / ``open`` markets.

        Results are cached with a configurable TTL (default 10 min).
        """
        ttl = int(os.getenv("KALSHI_EVENT_MARKETS_CACHE_TTL_SECONDS", "600"))
        now = time.time()
        if self._event_markets_cache and (now - self._event_markets_fetched_at) < ttl:
            return list(self._event_markets_cache)

        # --- Step 1: discover event tickers ---
        event_tickers: List[str] = []
        cursor: Optional[str] = None
        ev_limit = 200
        max_ev_pages = 5

        try:
            for _ in range(max_ev_pages):
                params: Dict[str, Any] = {"limit": ev_limit}
                if cursor:
                    params["cursor"] = cursor
                data = await self._get_json("/events", params=params)
                if not data:
                    break
                events = data.get("events", [])
                if not isinstance(events, list) or not events:
                    break
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    ticker = ev.get("event_ticker") or ev.get("ticker") or ""
                    if not ticker:
                        continue
                    t_upper = str(ticker).upper()
                    # Skip MVE (parlay) events
                    if t_upper.startswith("KXMVE"):
                        continue
                    # Skip sports + state-level political race events
                    if any(t_upper.startswith(p) for p in self._SKIP_EVENT_PREFIXES):
                        continue
                    event_tickers.append(str(ticker))
                    if len(event_tickers) >= self._MAX_EVENTS_PER_CYCLE:
                        break
                if len(event_tickers) >= self._MAX_EVENTS_PER_CYCLE:
                    break
                cursor = data.get("cursor") or data.get("next_cursor") or data.get("next")
                if not cursor or len(events) < ev_limit:
                    break
        except Exception as e:
            logger.error(f"[Kalshi] Event discovery failed: {e}")

        if not event_tickers:
            return []

        logger.info(
            f"[Kalshi] Discovered {len(event_tickers)} non-sports events for market lookup"
        )

        # --- Step 2: fetch markets for each event (low concurrency) ---
        odds_list: List[MarketOdds] = []
        concurrency = int(os.getenv("KALSHI_EVENT_CONCURRENCY", "2"))
        sem = asyncio.Semaphore(max(1, concurrency))

        async def _fetch_event_markets(ev_ticker: str) -> List[MarketOdds]:
            async with sem:
                try:
                    params_m: Dict[str, Any] = {
                        "event_ticker": ev_ticker,
                        "mve_filter": "exclude",
                        "limit": 100,
                    }
                    data_m = await self._get_json("/markets", params=params_m)
                    if not data_m:
                        return []
                    markets = data_m.get("markets", [])
                    if not isinstance(markets, list):
                        return []
                    result: List[MarketOdds] = []
                    for mkt in markets:
                        if not isinstance(mkt, dict):
                            continue
                        status = str(mkt.get("status", "")).lower()
                        if status not in {"open", "active"}:
                            continue
                        if str(mkt.get("ticker", "")).startswith("KXMVE"):
                            continue
                        if str(mkt.get("market_type", "")).lower() not in {"binary", ""}:
                            continue
                        result.extend(self._parse_market(mkt))
                    return result
                except Exception as e:
                    logger.debug(f"[Kalshi] Failed to fetch event {ev_ticker}: {e}")
                    return []

        tasks = [asyncio.create_task(_fetch_event_markets(t)) for t in event_tickers]
        for res in await asyncio.gather(*tasks):
            odds_list.extend(res)

        # De-duplicate by event_id (first seen wins)
        seen: set = set()
        deduped: List[MarketOdds] = []
        for o in odds_list:
            if o.event_id in seen:
                continue
            seen.add(o.event_id)
            deduped.append(o)

        self._event_markets_cache = deduped
        self._event_markets_fetched_at = time.time()
        logger.info(
            f"[Kalshi] Fetched {len(deduped)} event-based market odds "
            f"({len(event_tickers)} non-sports events)"
        )
        return list(deduped)

    async def _get_series_tickers_for_poll(self) -> List[str]:
        """Return a rotating slice of discovered series tickers for this poll cycle."""
        all_tickers = await self._get_all_series_tickers_cached()
        if not all_tickers:
            # Fall back to sports series only if discovery fails
            return list(dict(self.SPORT_SERIES).values())

        max_series = int(os.getenv("KALSHI_MAX_SERIES_PER_POLL", "25"))
        max_series = max(1, max_series)
        if len(all_tickers) <= max_series:
            return all_tickers

        start = self._series_rr_idx % len(all_tickers)
        end = start + max_series
        selected = (all_tickers[start:end] if end <= len(all_tickers)
                    else all_tickers[start:] + all_tickers[: (end % len(all_tickers))])
        self._series_rr_idx = (self._series_rr_idx + max_series) % len(all_tickers)
        return selected

    async def _get_all_series_tickers_cached(self) -> List[str]:
        ttl = int(os.getenv("KALSHI_SERIES_CACHE_TTL_SECONDS", "21600"))  # 6h
        now = time.time()
        if self._series_cache and (now - self._series_cache_fetched_at) < ttl:
            return self._series_cache

        tickers: List[str] = []
        cursor: Optional[str] = None
        limit = int(os.getenv("KALSHI_SERIES_PAGE_LIMIT", "200"))
        limit = max(1, min(limit, 200))

        try:
            while True:
                params: Dict[str, Any] = {"limit": limit}
                if cursor:
                    params["cursor"] = cursor
                data = await self._get_json("/series", params=params)
                if not data:
                    break

                series_list = data.get("series", [])
                if not isinstance(series_list, list) or not series_list:
                    break

                for s in series_list:
                    t = s.get("ticker") if isinstance(s, dict) else None
                    if not t:
                        continue
                    # Skip MVE series (parlays)
                    if str(t).startswith("KXMVE"):
                        continue
                    tickers.append(str(t))

                cursor = data.get("cursor") or data.get("next_cursor") or data.get("next")
                if not cursor:
                    break

        except Exception as e:
            logger.error(f"[Kalshi] Series discovery failed: {e}")

        # De-dupe while preserving order
        seen = set()
        deduped = []
        for t in tickers:
            if t in seen:
                continue
            seen.add(t)
            deduped.append(t)

        self._series_cache = deduped
        self._series_cache_fetched_at = now
        logger.info(f"[Kalshi] Discovered {len(deduped)} series tickers")
        return deduped

    async def _fetch_markets_for_series(self, series_ticker: str) -> List[MarketOdds]:
        """Fetch all open markets for a single series ticker (paginated)."""
        odds_list: List[MarketOdds] = []
        cursor: Optional[str] = None
        limit = int(os.getenv("KALSHI_MARKETS_PAGE_LIMIT", "200"))
        limit = max(1, min(limit, 200))

        max_pages = int(os.getenv("KALSHI_MAX_MARKET_PAGES_PER_SERIES", "2"))
        max_pages = max(1, max_pages)
        page_count = 0

        while True:
            page_count += 1
            params: Dict[str, Any] = {
                "status": "open",
                "limit": limit,
                "series_ticker": series_ticker,
            }
            if cursor:
                params["cursor"] = cursor

            data = await self._get_json("/markets", params=params)
            if not data:
                break
            markets = data.get("markets", [])
            if not isinstance(markets, list) or not markets:
                break

            for market in markets:
                if market.get("ticker", "").startswith("KXMVE"):
                    continue
                odds_list.extend(self._parse_market(market))

            cursor = data.get("cursor") or data.get("next_cursor") or data.get("next")
            if page_count >= max_pages:
                break
            if not cursor or len(markets) < limit:
                break

        return odds_list

    def _parse_market(self, market: Dict) -> List[MarketOdds]:
        """Parse Kalshi market into MarketOdds."""
        odds_list = []

        ticker = market.get("ticker", "unknown")
        title = market.get("title", "") or market.get("subtitle", "Unknown")
        subtitle = market.get("subtitle")
        if subtitle and subtitle not in {title, "Unknown"}:
            # Keep short; many Kalshi markets share the same title.
            title = f"{title} — {subtitle}"

        def _as_float(v: Any) -> Optional[float]:
            if v is None:
                return None
            try:
                return float(v)
            except Exception:
                return None

        # Prefer dollar fields when present (strings like "0.6300"); fall back
        # to cent integer fields.
        yes_bid_d = _as_float(market.get("yes_bid_dollars"))
        yes_ask_d = _as_float(market.get("yes_ask_dollars"))
        no_bid_d = _as_float(market.get("no_bid_dollars"))
        no_ask_d = _as_float(market.get("no_ask_dollars"))

        if yes_bid_d is not None and yes_ask_d is not None:
            yes_price = (yes_bid_d + yes_ask_d) / 2.0
        else:
            yes_bid = market.get("yes_bid", 0)  # cents
            yes_ask = market.get("yes_ask", 100)  # cents
            yes_price = (float(yes_bid) + float(yes_ask)) / 200.0

        if no_bid_d is not None and no_ask_d is not None:
            no_price = (no_bid_d + no_ask_d) / 2.0
        else:
            no_bid = market.get("no_bid", 0)  # cents
            no_ask = market.get("no_ask", 100)  # cents
            no_price = (float(no_bid) + float(no_ask)) / 200.0

        # Calculate decimal odds
        if 0 < yes_price < 1:
            odds_list.append(MarketOdds(
                event_id=f"kalshi-{ticker}",
                sport="prediction",
                # Keep more context for cross-platform matching; still bounded.
                market=title[:160],
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=round(1 / yes_price, 4),
                market_type="prediction",
                expires_at=self._parse_expiration(market),
            ))

        if 0 < no_price < 1:
            odds_list.append(MarketOdds(
                event_id=f"kalshi-{ticker}",
                sport="prediction",
                market=title[:160],
                bookmaker="kalshi",
                selection="No",
                odds_decimal=round(1 / no_price, 4),
                market_type="prediction",
                expires_at=self._parse_expiration(market),
            ))

        return odds_list

    def _parse_expiration(self, market: Dict) -> Optional[datetime]:
        """Parse market expiration date."""
        exp_str = market.get("expiration_time")
        if exp_str:
            try:
                return datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class PredictionMarketArbFinder:
    """
    Finds arbitrage opportunities between prediction markets and sportsbooks.

    Maps similar events across platforms and detects pricing discrepancies.
    """

    def __init__(self):
        self.polymarket = PolymarketAdapter()
        self.kalshi = KalshiAdapter()

    async def find_cross_market_arbs(
        self,
        sportsbook_odds: List[MarketOdds],
        sport: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Find arbitrage opportunities between prediction markets and sportsbooks.
        """
        arbs = []

        # Fetch prediction market odds
        poly_odds = await self.polymarket.fetch_markets(sport)
        kalshi_odds = await self.kalshi.fetch_markets(sport)

        # Combine all odds
        prediction_odds = poly_odds + kalshi_odds

        # Try to match events and find arbs
        for sb_odds in sportsbook_odds:
            matches = self._find_matching_prediction(sb_odds, prediction_odds)
            for pred_odds in matches:
                arb = self._check_arbitrage(sb_odds, pred_odds)
                if arb:
                    arbs.append(arb)

        return arbs

    def _find_matching_prediction(
        self,
        sb_odds: MarketOdds,
        prediction_odds: List[MarketOdds]
    ) -> List[MarketOdds]:
        """Find prediction market odds that match a sportsbook event."""
        matches = []

        # Extract key terms from sportsbook odds
        sb_terms = self._extract_terms(sb_odds.market + " " + sb_odds.selection)

        for pred in prediction_odds:
            pred_terms = self._extract_terms(pred.market + " " + pred.selection)

            # Check for significant term overlap
            overlap = len(sb_terms & pred_terms)
            if overlap >= 2:  # At least 2 common terms
                matches.append(pred)

        return matches

    def _extract_terms(self, text: str) -> set:
        """Extract searchable terms from text."""
        # Remove common words and extract key terms
        text = text.lower()
        words = re.findall(r'\b[a-z]+\b', text)
        stopwords = {"the", "a", "an", "to", "win", "will", "be", "is", "vs", "at"}
        return set(w for w in words if w not in stopwords and len(w) > 2)

    def _check_arbitrage(
        self,
        sb_odds: MarketOdds,
        pred_odds: MarketOdds
    ) -> Optional[Dict[str, Any]]:
        """Check if two odds create an arbitrage opportunity."""
        # Calculate implied probabilities
        sb_prob = 1 / sb_odds.odds_decimal
        pred_prob = 1 / pred_odds.odds_decimal

        # Check for arb (probabilities sum to < 1)
        total_prob = sb_prob + (1 - pred_prob)  # Opposing sides

        if total_prob < 0.98:  # At least 2% edge
            edge = (1 - total_prob) * 100
            return {
                "type": "cross_market",
                "edge_percentage": round(edge, 2),
                "leg1": {
                    "bookmaker": sb_odds.bookmaker,
                    "market": sb_odds.market,
                    "selection": sb_odds.selection,
                    "odds": sb_odds.odds_decimal,
                },
                "leg2": {
                    "bookmaker": pred_odds.bookmaker,
                    "market": pred_odds.market,
                    "selection": pred_odds.selection,
                    "odds": pred_odds.odds_decimal,
                },
            }

        return None

    async def close(self):
        """Close all adapters."""
        await self.polymarket.close()
        await self.kalshi.close()


@dataclass(frozen=True)
class _PmGroup:
    bookmaker: str
    source_event_id: str
    market: str
    expires_at: Optional[datetime]
    norm_text: str
    tokens: Set[str]


class PredictionMarketEventUnifier:
    """Fuzzy-match equivalent Polymarket and Kalshi markets and unify IDs.

    This is intentionally generic (no per-bank/per-series hardcoding). It uses
    lightweight text normalization + token overlap + sequence similarity.
    """

    _STOPWORDS = {
        "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "with",
        "will", "be", "is", "are", "was", "were", "by", "before", "after",
        "vs", "at", "yes", "no",
        # Very common verbs/nouns that add noise across platforms.
        "win", "wins", "winner", "match", "round", "game",
    }

    def __init__(self, min_match_score: Optional[float] = None):
        # Keep a conservative default to avoid false cross-market merges.
        # Tune via env var PM_MATCH_MIN_SCORE when experimenting.
        self.min_match_score = float(os.getenv("PM_MATCH_MIN_SCORE", str(min_match_score or 0.74)))
        self.max_candidates_per_poly = int(os.getenv("PM_MATCH_MAX_CANDIDATES", "250"))
        self.max_anchor_tokens = int(os.getenv("PM_MATCH_MAX_ANCHOR_TOKENS", "8"))
        self.common_token_df_pct = float(os.getenv("PM_MATCH_COMMON_TOKEN_DF_PCT", "0.05"))

        self.debug = str(os.getenv("PM_MATCH_DEBUG", "false")).lower() in {"1", "true", "yes", "y", "on"}
        self.debug_topk = int(os.getenv("PM_MATCH_DEBUG_TOPK", "5"))

        # Populated per unify() call (best candidate seen, even if below threshold).
        self._last_best_candidate: Optional[Tuple[float, str, str]] = None
        self._last_debug_top: List[Tuple[float, str, str]] = []

    def unify(self, poly_odds: List[MarketOdds], kalshi_odds: List[MarketOdds]) -> Tuple[List[MarketOdds], Dict[str, Any]]:
        """Return a combined list where matched markets share event_id and market string."""
        poly_groups = self._build_groups(poly_odds)
        kalshi_groups = self._build_groups(kalshi_odds)

        matches = self._match_groups(poly_groups, kalshi_groups)
        mapping: Dict[str, Tuple[str, str]] = {}  # source_event_id -> (unified_event_id, unified_market)

        for poly_g, kalshi_g, score in matches:
            canonical_market = kalshi_g.market[:160]
            canonical_exp = kalshi_g.expires_at or poly_g.expires_at
            unified_event_id = self._stable_pred_id(canonical_market, canonical_exp)
            mapping[poly_g.source_event_id] = (unified_event_id, canonical_market)
            mapping[kalshi_g.source_event_id] = (unified_event_id, canonical_market)

        def _apply(odds: Iterable[MarketOdds]) -> List[MarketOdds]:
            out: List[MarketOdds] = []
            for o in odds:
                m = mapping.get(o.event_id)
                if not m:
                    out.append(o)
                    continue
                unified_event_id, unified_market = m
                out.append(o.model_copy(update={"event_id": unified_event_id, "market": unified_market}))
            return out

        unified = _apply(poly_odds) + _apply(kalshi_odds)
        meta = {
            "poly_groups": len(poly_groups),
            "kalshi_groups": len(kalshi_groups),
            "matched_pairs": len(matches),
            "best_candidate_score": (self._last_best_candidate[0] if self._last_best_candidate else None),
        }
        if matches:
            logger.info(
                "[PredictionMarketUnifier] matched %s/%s poly groups with %s kalshi groups",
                len(matches), len(poly_groups), len(kalshi_groups),
            )
            # Log a few examples to aid validation/debugging.
            for poly_g, kalshi_g, score in matches[:5]:
                logger.info(
                    "[PredictionMarketUnifier] example score=%.3f poly='%s' kalshi='%s'",
                    score,
                    (poly_g.market or "")[:120],
                    (kalshi_g.market or "")[:120],
                )
        elif self.debug and self._last_best_candidate:
            bs, pm, km = self._last_best_candidate
            logger.info(
                "[PredictionMarketUnifier] no matches (min=%.2f). Best candidate score=%.3f poly='%s' kalshi='%s'",
                self.min_match_score,
                bs,
                (pm or "")[:120],
                (km or "")[:120],
            )
            for s, pm2, km2 in (self._last_debug_top or [])[: max(0, self.debug_topk)]:
                logger.info(
                    "[PredictionMarketUnifier] top-candidate score=%.3f poly='%s' kalshi='%s'",
                    s,
                    (pm2 or "")[:120],
                    (km2 or "")[:120],
                )
        return unified, meta

    def _build_groups(self, odds: List[MarketOdds]) -> List[_PmGroup]:
        by_event: Dict[str, List[MarketOdds]] = {}
        for o in odds:
            by_event.setdefault(o.event_id, []).append(o)

        groups: List[_PmGroup] = []
        for event_id, entries in by_event.items():
            first = entries[0]
            market = first.market or ""
            norm_text = self._normalize_text(market)
            tokens = self._tokenize(norm_text)
            groups.append(_PmGroup(
                bookmaker=first.bookmaker,
                source_event_id=event_id,
                market=market,
                expires_at=first.expires_at,
                norm_text=norm_text,
                tokens=tokens,
            ))
        return groups

    def _match_groups(self, poly_groups: List[_PmGroup], kalshi_groups: List[_PmGroup]) -> List[Tuple[_PmGroup, _PmGroup, float]]:
        from collections import defaultdict

        # Build inverted index for Kalshi tokens -> candidate group indices
        index: Dict[str, List[int]] = defaultdict(list)
        df: Dict[str, int] = defaultdict(int)  # document frequency across Kalshi groups
        for i, g in enumerate(kalshi_groups):
            # Count each token once per group for DF.
            for tok in g.tokens:
                if tok in self._STOPWORDS:
                    continue
                df[tok] += 1
                index[tok].append(i)

        common_df_cutoff = max(1, int(self.common_token_df_pct * max(1, len(kalshi_groups))))

        def _has_digit(tok: str) -> bool:
            return any("0" <= ch <= "9" for ch in tok)

        candidate_pairs: List[Tuple[float, int, int]] = []
        best_seen: Tuple[float, int, int] = (0.0, -1, -1)
        debug_top: List[Tuple[float, int, int]] = []  # keep a small top-K for diagnostics
        for pi, pg in enumerate(poly_groups):
            cand_idxs: Set[int] = set()

            # Prefer "anchor" tokens that are rare on Kalshi; reduces noise (e.g., matching on 'trump').
            # Tokens absent in Kalshi get a very high DF so they sort to the end.
            anchor_toks = sorted(
                [t for t in pg.tokens if t not in self._STOPWORDS],
                key=lambda t: df.get(t, 10**9),
            )

            for tok in anchor_toks[: max(1, self.max_anchor_tokens)]:
                for ki in index.get(tok, []):
                    cand_idxs.add(ki)
                if len(cand_idxs) >= self.max_candidates_per_poly:
                    break

            # If anchors produced nothing (rare), fall back to using all tokens.
            if not cand_idxs:
                for tok in pg.tokens:
                    if tok in self._STOPWORDS:
                        continue
                    for ki in index.get(tok, []):
                        cand_idxs.add(ki)
                    if len(cand_idxs) >= self.max_candidates_per_poly:
                        break

            if not cand_idxs:
                continue

            scored: List[Tuple[float, int]] = []
            for ki in cand_idxs:
                kg = kalshi_groups[ki]

                overlap = (pg.tokens & kg.tokens)
                overlap_non_stop = {t for t in overlap if t not in self._STOPWORDS}
                if len(overlap_non_stop) < 2:
                    # Require at least 2 shared informative tokens to avoid garbage matches.
                    # Allow a single-token overlap only when it's a numeric anchor.
                    if not any(_has_digit(t) for t in overlap_non_stop):
                        continue

                # If all overlapping tokens are extremely common on Kalshi, demand stronger evidence.
                has_rare_overlap = any(df.get(t, 10**9) <= common_df_cutoff for t in overlap_non_stop)
                shared_numeric = any(_has_digit(t) for t in overlap_non_stop)
                if not has_rare_overlap and not shared_numeric and len(overlap_non_stop) < 3:
                    continue

                score = self._similarity(pg, kg)

                if score > best_seen[0]:
                    best_seen = (score, pi, ki)

                if self.debug and self.debug_topk > 0:
                    if len(debug_top) < self.debug_topk:
                        debug_top.append((score, pi, ki))
                    else:
                        # Replace the current minimum if this score is higher.
                        min_i = min(range(len(debug_top)), key=lambda i: debug_top[i][0])
                        if score > debug_top[min_i][0]:
                            debug_top[min_i] = (score, pi, ki)

                if score >= self.min_match_score:
                    scored.append((score, ki))

            # Keep top few per poly group (reduces global sort cost)
            scored.sort(reverse=True, key=lambda x: x[0])
            for score, ki in scored[:5]:
                candidate_pairs.append((score, pi, ki))

        # Greedy global assignment by score
        candidate_pairs.sort(reverse=True, key=lambda x: x[0])
        used_poly: Set[int] = set()
        used_kalshi: Set[int] = set()
        matches: List[Tuple[_PmGroup, _PmGroup, float]] = []

        for score, pi, ki in candidate_pairs:
            if pi in used_poly or ki in used_kalshi:
                continue
            used_poly.add(pi)
            used_kalshi.add(ki)
            matches.append((poly_groups[pi], kalshi_groups[ki], score))

        # Store per-call debug info.
        if best_seen[1] >= 0 and best_seen[2] >= 0:
            self._last_best_candidate = (
                best_seen[0],
                poly_groups[best_seen[1]].market,
                kalshi_groups[best_seen[2]].market,
            )
        else:
            self._last_best_candidate = None

        if self.debug and debug_top:
            debug_top.sort(reverse=True, key=lambda x: x[0])
            self._last_debug_top = [
                (s, poly_groups[pi].market, kalshi_groups[ki].market) for (s, pi, ki) in debug_top
            ]
        else:
            self._last_debug_top = []

        return matches

    def _similarity(self, a: _PmGroup, b: _PmGroup) -> float:
        # Token overlap
        a_toks = {t for t in a.tokens if t not in self._STOPWORDS}
        b_toks = {t for t in b.tokens if t not in self._STOPWORDS}
        inter = len(a_toks & b_toks)
        union = len(a_toks | b_toks) or 1
        jaccard = inter / union
        containment = inter / (min(len(a_toks), len(b_toks)) or 1)

        # Sequence similarity on normalized strings
        seq = SequenceMatcher(None, a.norm_text, b.norm_text).ratio()

        # Combined score: containment helps when one platform is more verbose.
        score = 0.45 * seq + 0.35 * containment + 0.20 * jaccard

        # Small bonus when key numeric anchors overlap (years, strike prices, etc.)
        def _has_digit(tok: str) -> bool:
            return any("0" <= ch <= "9" for ch in tok)

        if any(_has_digit(t) for t in (a_toks & b_toks)):
            score = min(1.0, score + 0.05)

        # Expiration-date penalty (if both available and differ by a lot)
        if a.expires_at and b.expires_at:
            delta_days = abs((a.expires_at.date() - b.expires_at.date()).days)
            if delta_days >= 3:
                score *= 0.85

        return score

    def _normalize_text(self, text: str) -> str:
        s = (text or "").lower()
        s = re.sub(r"[^a-z0-9 ]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _tokenize(self, norm_text: str) -> Set[str]:
        toks = {t for t in norm_text.split(" ") if len(t) >= 3 and t not in self._STOPWORDS}
        return toks

    def _stable_pred_id(self, canonical_market: str, expires_at: Optional[datetime]) -> str:
        base = (canonical_market or "").strip().lower()
        if expires_at:
            base = f"{base}|{expires_at.date().isoformat()}"
        digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
        return f"pred-{digest}"

