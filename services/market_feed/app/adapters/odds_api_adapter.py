"""
The Odds API adapter for pre-game odds.

This adapter fetches odds from The Odds API (https://the-odds-api.com/),
a third-party service that aggregates odds from 40+ sportsbooks including
FanDuel, DraftKings, and Fanatics.

Benefits:
- No browser automation needed
- No login/2FA required
- No anti-bot detection issues
- Reliable and fast

Limitations:
- Live odds have 5-30 second delay depending on plan
- Monthly cost ($79-199/month for production use)
- Rate limits apply
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from shared.schemas import MarketOdds, ScrapeResult

logger = logging.getLogger(__name__)

# The Odds API base URL
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"

# Sport keys for The Odds API
SPORT_KEYS = {
    "nfl": "americanfootball_nfl",
    "nba": "basketball_nba",
    "mlb": "baseball_mlb",
    "nhl": "icehockey_nhl",
    "ncaaf": "americanfootball_ncaaf",
    "ncaab": "basketball_ncaab",
}

# Bookmaker keys we care about (CT legal books)
CT_BOOKMAKERS = ["fanduel", "draftkings", "fanatics"]


class OddsAPIAdapter:
    """
    Adapter for The Odds API.
    
    Fetches pre-game odds from FanDuel, DraftKings, Fanatics via REST API.
    No browser automation, no login required.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("ODDS_API_KEY")
        if not self.api_key:
            logger.warning("ODDS_API_KEY not set - adapter will not work")
        
        self._client = httpx.Client(timeout=30.0)
        self._requests_remaining: Optional[int] = None
        self._requests_used: Optional[int] = None
    
    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()
    
    @property
    def requests_remaining(self) -> Optional[int]:
        """Number of API requests remaining this month."""
        return self._requests_remaining
    
    def get_odds(
        self,
        sports: Optional[List[str]] = None,
        bookmakers: Optional[List[str]] = None,
        markets: Optional[List[str]] = None,
    ) -> ScrapeResult:
        """
        Fetch odds from The Odds API.
        
        Args:
            sports: List of sports to fetch (e.g., ["nfl", "nba"]). Default: all major sports.
            bookmakers: List of bookmakers to include. Default: CT legal books.
            markets: List of market types. Default: ["h2h"] (moneyline).
        
        Returns:
            ScrapeResult with normalized MarketOdds.
        """
        if not self.api_key:
            return ScrapeResult(
                bookmaker="odds_api",
                success=False,
                error="ODDS_API_KEY not configured",
            )
        
        sports = sports or list(SPORT_KEYS.keys())
        bookmakers = bookmakers or CT_BOOKMAKERS
        markets = markets or ["h2h", "spreads", "totals"]
        
        all_odds: List[MarketOdds] = []
        
        for sport in sports:
            sport_key = SPORT_KEYS.get(sport.lower())
            if not sport_key:
                logger.warning(f"Unknown sport: {sport}")
                continue
            
            try:
                odds = self._fetch_sport_odds(sport_key, sport, bookmakers, markets)
                all_odds.extend(odds)
            except Exception as e:
                logger.error(f"Error fetching {sport} odds: {e}")
        
        return ScrapeResult(
            bookmaker="odds_api",
            success=True,
            odds=all_odds,
            scraped_at=datetime.utcnow(),
        )
    
    def _fetch_sport_odds(
        self,
        sport_key: str,
        sport_name: str,
        bookmakers: List[str],
        markets: List[str],
    ) -> List[MarketOdds]:
        """Fetch odds for a single sport."""
        odds_list: List[MarketOdds] = []
        
        url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/odds"
        params = {
            "apiKey": self.api_key,
            "regions": "us",
            "markets": ",".join(markets),
            "bookmakers": ",".join(bookmakers),
            "oddsFormat": "decimal",
        }
        
        response = self._client.get(url, params=params)
        
        # Track rate limit info from headers
        self._requests_remaining = int(response.headers.get("x-requests-remaining", 0))
        self._requests_used = int(response.headers.get("x-requests-used", 0))
        
        if response.status_code == 401:
            raise ValueError("Invalid ODDS_API_KEY")
        elif response.status_code == 429:
            raise ValueError("Rate limit exceeded")
        
        response.raise_for_status()
        events = response.json()
        
        logger.info(f"[odds_api] Fetched {len(events)} {sport_name} events "
                    f"(requests remaining: {self._requests_remaining})")
        
        for event in events:
            event_odds = self._parse_event(event, sport_name)
            odds_list.extend(event_odds)

        return odds_list

    def _parse_event(self, event: Dict[str, Any], sport: str) -> List[MarketOdds]:
        """Parse a single event from The Odds API response."""
        odds_list: List[MarketOdds] = []

        event_id = event.get("id", "")
        home_team = event.get("home_team", "")
        away_team = event.get("away_team", "")
        event_name = f"{away_team} @ {home_team}"
        commence_time = event.get("commence_time", "")

        for bookmaker in event.get("bookmakers", []):
            book_key = bookmaker.get("key", "").lower()

            for market in bookmaker.get("markets", []):
                market_key = market.get("key", "")
                market_type = self._normalize_market(market_key)

                for outcome in market.get("outcomes", []):
                    selection = outcome.get("name", "")
                    odds_decimal = outcome.get("price")
                    point = outcome.get("point")  # For spreads/totals

                    if selection and odds_decimal and odds_decimal > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport.lower(),
                            market=market_type,
                            market_type=market_type,
                            bookmaker=book_key,
                            selection=selection,
                            odds_decimal=float(odds_decimal),
                            line=float(point) if point is not None else None,
                            is_live=False,
                            captured_at=datetime.utcnow(),
                        ))

        return odds_list

    def _normalize_market(self, market_key: str) -> str:
        """Normalize market key to standard format."""
        market_map = {
            "h2h": "moneyline",
            "spreads": "spread",
            "totals": "total",
        }
        return market_map.get(market_key, market_key)

    def get_sports(self) -> List[Dict[str, Any]]:
        """Get list of available sports from The Odds API."""
        if not self.api_key:
            return []

        url = f"{ODDS_API_BASE_URL}/sports"
        params = {"apiKey": self.api_key}

        response = self._client.get(url, params=params)
        response.raise_for_status()

        return response.json()

    def get_live_odds(
        self,
        sports: Optional[List[str]] = None,
        bookmakers: Optional[List[str]] = None,
    ) -> ScrapeResult:
        """
        Fetch live/in-play odds.

        Note: Live odds from The Odds API have 5-30 second delay.
        For true real-time live odds, use the InterceptingAdapter.
        """
        if not self.api_key:
            return ScrapeResult(
                bookmaker="odds_api",
                success=False,
                error="ODDS_API_KEY not configured",
            )

        sports = sports or list(SPORT_KEYS.keys())
        bookmakers = bookmakers or CT_BOOKMAKERS

        all_odds: List[MarketOdds] = []

        for sport in sports:
            sport_key = SPORT_KEYS.get(sport.lower())
            if not sport_key:
                continue

            try:
                url = f"{ODDS_API_BASE_URL}/sports/{sport_key}/odds-live"
                params = {
                    "apiKey": self.api_key,
                    "regions": "us",
                    "markets": "h2h",
                    "bookmakers": ",".join(bookmakers),
                    "oddsFormat": "decimal",
                }

                response = self._client.get(url, params=params)

                if response.status_code == 404:
                    # No live events for this sport
                    continue

                response.raise_for_status()
                events = response.json()

                for event in events:
                    event_odds = self._parse_event(event, sport)
                    # Mark as live
                    for odds in event_odds:
                        odds.is_live = True
                    all_odds.extend(event_odds)

            except Exception as e:
                logger.error(f"Error fetching live {sport} odds: {e}")

        return ScrapeResult(
            bookmaker="odds_api",
            success=True,
            odds=all_odds,
            scraped_at=datetime.utcnow(),
        )

