"""
OddsPapi API adapter for real-time sports odds.

OddsPapi provides 300+ bookmakers with WebSocket support and better DK/FD coverage.
This is a test implementation to compare against The Odds API.
"""
from __future__ import annotations

import logging
import httpx
from datetime import datetime
from typing import List, Optional, Dict, Any

from shared.schemas import MarketOdds, ScrapeResult

logger = logging.getLogger(__name__)


class OddsPapiAdapter:
    """
    Adapter for OddsPapi - 300+ bookmakers with real-time WebSocket support.
    
    Test implementation to evaluate coverage vs The Odds API.
    """
    
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.oddspapi.io/v1"
        self.client = httpx.Client(timeout=30.0)
        self.requests_remaining = None
        
        # Sport mappings for OddsPapi
        self.sport_mapping = {
            "nfl": "american-football_nfl",
            "nba": "basketball_nba", 
            "mlb": "baseball_mlb",
            "nhl": "ice-hockey_nhl",
            "ncaaf": "american-football_ncaaf",
            "ncaab": "basketball_ncaab",
        }
        
        # Market mappings
        self.market_mapping = {
            "moneyline": "moneyline",
            "spread": "spread", 
            "total": "total",
            "h2h": "moneyline",
            "spreads": "spread",
            "totals": "total"
        }
        
        logger.info("[OddsPapi] Adapter initialized")
    
    def get_odds(
        self, 
        sports: List[str], 
        bookmakers: List[str] = None, 
        markets: List[str] = None
    ) -> ScrapeResult:
        """
        Fetch odds from OddsPapi for specified sports and bookmakers.
        
        Args:
            sports: List of sport keys (nfl, nba, etc.)
            bookmakers: List of bookmaker keys (draftkings, fanduel)
            markets: List of market types (moneyline, spread, total)
        """
        try:
            if not bookmakers:
                bookmakers = ["draftkings", "fanduel"]
            
            if not markets:
                markets = ["moneyline", "spread", "total"]
            
            all_odds = []
            
            for sport in sports:
                # Map internal sport to OddsPapi sport
                api_sport = self.sport_mapping.get(sport.lower())
                if not api_sport:
                    logger.warning(f"[OddsPapi] Unknown sport: {sport}")
                    continue
                
                # Map markets to OddsPapi format
                api_markets = []
                for market in markets:
                    mapped_market = self.market_mapping.get(market.lower())
                    if mapped_market:
                        api_markets.append(mapped_market)
                
                if not api_markets:
                    api_markets = ["moneyline"]  # Default
                
                logger.info(f"[OddsPapi] Fetching {sport} odds for bookmakers: {bookmakers}")
                
                # Fetch odds for this sport
                sport_odds = self._fetch_sport_odds(api_sport, bookmakers, api_markets)
                all_odds.extend(sport_odds)
            
            logger.info(f"[OddsPapi] Retrieved {len(all_odds)} total odds")
            logger.info(f"[OddsPapi] API requests remaining: {self.requests_remaining}")
            
            return ScrapeResult(
                success=True,
                odds=all_odds,
                error=None,
                timestamp=datetime.utcnow()
            )
            
        except Exception as e:
            logger.error(f"[OddsPapi] Error fetching odds: {e}")
            return ScrapeResult(
                success=False,
                odds=[],
                error=str(e),
                timestamp=datetime.utcnow()
            )
    
    def _fetch_sport_odds(
        self, 
        sport: str, 
        bookmakers: List[str], 
        markets: List[str]
    ) -> List[MarketOdds]:
        """Fetch odds for a specific sport from OddsPapi."""
        try:
            # Build request parameters
            params = {
                "apiKey": self.api_key,
                "sport": sport,
                "bookmakers": ",".join(bookmakers),
                "markets": ",".join(markets),
                "format": "json"
            }
            
            url = f"{self.base_url}/odds"
            response = self.client.get(url, params=params)
            
            # Update requests remaining from headers
            if "x-requests-remaining" in response.headers:
                self.requests_remaining = int(response.headers["x-requests-remaining"])
            
            if response.status_code == 429:
                logger.error(f"[OddsPapi] Rate limit exceeded for {sport}")
                return []
            
            if response.status_code != 200:
                logger.error(f"[OddsPapi] HTTP {response.status_code} for {sport}: {response.text}")
                return []
            
            data = response.json()
            logger.info(f"[OddsPapi] Fetched {len(data)} {sport} events")
            
            # Convert to MarketOdds format
            odds_list = []
            for event in data:
                event_odds = self._convert_event_to_odds(event, sport)
                odds_list.extend(event_odds)
            
            return odds_list
            
        except Exception as e:
            logger.error(f"[OddsPapi] Error fetching {sport} odds: {e}")
            return []
    
    def _convert_event_to_odds(self, event: Dict[str, Any], sport: str) -> List[MarketOdds]:
        """Convert OddsPapi event data to MarketOdds format."""
        odds_list = []
        
        try:
            event_id = event.get("id", "unknown")
            home_team = event.get("home_team", "")
            away_team = event.get("away_team", "")
            commence_time = event.get("commence_time", "")
            
            bookmakers = event.get("bookmakers", [])
            
            for bookmaker_data in bookmakers:
                bookmaker = bookmaker_data.get("key", "")
                if bookmaker not in ["draftkings", "fanduel"]:
                    continue  # Only process DK/FD
                
                markets = bookmaker_data.get("markets", [])
                
                for market_data in markets:
                    market_key = market_data.get("key", "")
                    outcomes = market_data.get("outcomes", [])
                    
                    for outcome in outcomes:
                        odds = MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            home_team=home_team,
                            away_team=away_team,
                            bookmaker=bookmaker,
                            market_type=market_key,
                            selection=outcome.get("name", ""),
                            odds_decimal=float(outcome.get("price", 1.0)),
                            timestamp=datetime.utcnow(),
                            commence_time=commence_time
                        )
                        odds_list.append(odds)
            
        except Exception as e:
            logger.error(f"[OddsPapi] Error converting event {event.get('id', 'unknown')}: {e}")
        
        return odds_list
    
    def close(self):
        """Close the HTTP client."""
        if self.client:
            self.client.close()
