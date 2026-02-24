"""
Wrapper adapter to make OddsAPIAdapter compatible with BaseFeedAdapter interface.

This allows The Odds API to be used as a drop-in replacement for traditional
sportsbook scrapers while maintaining the same interface.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import List

from shared.schemas import BookmakerCredentials, FeedConfig, MarketOdds, ScrapeResult, SessionStatus
from .base import BaseFeedAdapter
from .odds_api_adapter import OddsAPIAdapter

logger = logging.getLogger(__name__)


class OddsAPIWrapperAdapter(BaseFeedAdapter):
    """
    Wrapper that makes OddsAPIAdapter compatible with BaseFeedAdapter interface.
    
    This allows The Odds API to be used seamlessly within the existing
    session management framework.
    """
    
    def __init__(
        self,
        odds_api_adapter: OddsAPIAdapter,
        config: FeedConfig,
        credentials: BookmakerCredentials,
    ):
        super().__init__(config, credentials)
        self.odds_api = odds_api_adapter
        self.session_status.logged_in = True  # No login required for API
        self.session_status.session_valid = True
        
        # Map config sports to Odds API sports
        self.sport_mapping = {
            "nfl": "nfl",
            "nba": "nba", 
            "mlb": "mlb",
            "nhl": "nhl",
            "ncaaf": "ncaaf",
            "ncaab": "ncaab",
        }
        
        # CT legal bookmakers
        self.ct_bookmakers = ["fanduel", "draftkings"]
        
        logger.info(f"[{self.bookmaker}] OddsAPI wrapper initialized for CT sportsbooks")
    
    def initialize(self) -> bool:
        """Initialize the adapter (no-op for API)."""
        return True
    
    def _perform_login(self) -> bool:
        """No login required for The Odds API."""
        return True
    
    def _scrape_odds(self) -> List[MarketOdds]:
        """Scrape odds using The Odds API."""
        try:
            # Map config sports to API sports
            api_sports = []
            for sport in self.config.sports:
                if sport.lower() in self.sport_mapping:
                    api_sports.append(self.sport_mapping[sport.lower()])
            
            if not api_sports:
                logger.warning(f"[{self.bookmaker}] No supported sports found in config: {self.config.sports}")
                return []
            
            # Map config markets to API markets
            api_markets = []
            for market in self.config.markets:
                if market.lower() == "moneyline":
                    api_markets.append("h2h")
                elif market.lower() == "spread":
                    api_markets.append("spreads")
                elif market.lower() == "total":
                    api_markets.append("totals")
            
            if not api_markets:
                api_markets = ["h2h"]  # Default to moneyline
            
            logger.info(f"[{self.bookmaker}] Fetching odds for sports={api_sports}, markets={api_markets}")
            
            # Fetch odds from The Odds API
            result = self.odds_api.get_odds(
                sports=api_sports,
                bookmakers=self.ct_bookmakers,
                markets=api_markets
            )
            
            if not result.success:
                logger.error(f"[{self.bookmaker}] Odds API error: {result.error}")
                return []
            
            # Filter odds for this specific bookmaker
            bookmaker_odds = []
            if result.odds:
                for odds in result.odds:
                    if odds.bookmaker.lower() == self.bookmaker.lower():
                        bookmaker_odds.append(odds)
            
            logger.info(f"[{self.bookmaker}] Retrieved {len(bookmaker_odds)} odds from Odds API")
            logger.info(f"[{self.bookmaker}] API requests remaining: {self.odds_api.requests_remaining}")
            
            return bookmaker_odds
            
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error fetching from Odds API: {e}")
            return []
    
    def close(self) -> None:
        """Close the adapter."""
        if self.odds_api:
            self.odds_api.close()
        super().close()
    
    def _is_session_expired(self) -> bool:
        """API sessions don't expire."""
        return False
