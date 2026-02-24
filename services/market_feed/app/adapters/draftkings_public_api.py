"""
DraftKings Public API Adapter - Unauthenticated Odds Ingestion.

This adapter uses DraftKings' public API endpoints that do NOT require authentication.
It runs in parallel with existing authenticated adapters without replacing them.

Public API Endpoints:
- Navigation: https://sportsbook-nash.draftkings.com/sites/US-SB/api/v5/navigation?format=json
- EventGroups: https://sportsbook-nash.draftkings.com/sites/US-SB/api/v5/eventgroups/{eventGroupId}?format=json

NO login, NO cookies, NO browser automation required.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from shared.schemas import (
    BookmakerCredentials,
    FeedConfig,
    MarketOdds,
    ScrapeResult,
    SessionStatus,
)

logger = logging.getLogger(__name__)


class DraftKingsPublicAPIAdapter:
    """
    DraftKings Public API adapter - NO authentication required.
    
    This adapter:
    - Uses public DraftKings API endpoints
    - Does NOT require login or cookies
    - Runs in parallel with authenticated adapters
    - Reuses existing _parse_draftkings_json() logic
    """
    
    # Public API base URL
    BASE_URL = "https://sportsbook-nash.draftkings.com/sites/US-SB/api/v5"
    
    # Required headers to mimic browser requests
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Origin": "https://sportsbook.draftkings.com",
        "Referer": "https://sportsbook.draftkings.com/",
    }
    
    def __init__(self, config: FeedConfig, credentials: BookmakerCredentials):
        self.config = config
        self.credentials = credentials
        self.bookmaker = "draftkings"
        
        self.session_status = SessionStatus(
            bookmaker=self.bookmaker,
            logged_in=True,  # Public API doesn't need login
            session_valid=True,
        )
        
        self._scrape_count = 0
        self._error_count = 0
        
        logger.info("[DK PUBLIC] DraftKings Public API Adapter initialized")
    
    def initialize(self) -> None:
        """Initialize adapter (no-op for public API)."""
        logger.info("[DK PUBLIC] DK PUBLIC INGESTION ACTIVE")
        self.session_status.logged_in = True
        self.session_status.session_valid = True
        self.session_status.last_login_at = datetime.utcnow()
    
    def close(self) -> None:
        """Close adapter (no-op for public API)."""
        logger.info("[DK PUBLIC] Closing DraftKings Public API adapter")
    
    def login(self) -> bool:
        """Login (no-op for public API - always returns True)."""
        logger.info("[DK PUBLIC] Public API does not require login")
        self.session_status.logged_in = True
        self.session_status.session_valid = True
        return True
    
    def scrape(self) -> ScrapeResult:
        """Scrape odds from DraftKings public API."""
        start_time = time.time()
        
        try:
            logger.info("[DK PUBLIC] Starting public API scrape")
            
            # Step 1: Get navigation data to find eventGroupIds
            event_groups = self._fetch_navigation()
            
            if not event_groups:
                logger.warning("[DK PUBLIC] No event groups found in navigation")
                return ScrapeResult(
                    bookmaker=self.bookmaker,
                    success=True,
                    odds=[],
                    duration_ms=int((time.time() - start_time) * 1000),
                )
            
            logger.info(f"[DK PUBLIC] DK PUBLIC EVENTGROUPS DETECTED: {len(event_groups)}")
            
            # Step 2: Fetch odds for each eventGroup
            all_odds: List[MarketOdds] = []
            for event_group in event_groups:
                event_group_id = event_group.get("eventGroupId")
                sport = event_group.get("sport", "unknown")
                
                if not event_group_id:
                    continue
                
                odds = self._fetch_eventgroup_odds(event_group_id, sport)
                all_odds.extend(odds)
            
            duration_ms = int((time.time() - start_time) * 1000)
            self._scrape_count += 1
            self.session_status.last_activity_at = datetime.utcnow()
            
            logger.info(f"[DK PUBLIC] DK PUBLIC ODDS INGESTED: {len(all_odds)} odds from {len(event_groups)} event groups")
            
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=True,
                odds=all_odds,
                duration_ms=duration_ms,
                scraped_at=datetime.utcnow(),
            )
            
        except Exception as e:
            self._error_count += 1
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[DK PUBLIC] Scrape error: {e}")
            
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

    def _fetch_navigation(self) -> List[Dict[str, Any]]:
        """
        Fetch navigation data from DraftKings public API.

        Returns list of event groups with eventGroupId and sport info.
        """
        url = f"{self.BASE_URL}/navigation?format=json"

        try:
            logger.debug(f"[DK PUBLIC] Fetching navigation from {url}")
            response = requests.get(url, headers=self.HEADERS, timeout=10)
            response.raise_for_status()

            data = response.json()
            event_groups = []

            # Navigate through the navigation structure
            # Structure: navigation -> sports -> eventGroups
            navigation = data.get("navigation", [])

            for nav_item in navigation:
                # Look for sports sections
                sports = nav_item.get("sports", [])

                for sport_item in sports:
                    sport_name = sport_item.get("name", "").lower()

                    # Extract eventGroups from this sport
                    for event_group in sport_item.get("eventGroups", []):
                        event_group_id = event_group.get("eventGroupId")

                        if event_group_id:
                            event_groups.append({
                                "eventGroupId": event_group_id,
                                "sport": self._normalize_sport(sport_name),
                                "name": event_group.get("name", ""),
                                "isLive": event_group.get("isLive", False),
                            })

            logger.info(f"[DK PUBLIC] Found {len(event_groups)} event groups in navigation")
            return event_groups

        except Exception as e:
            logger.error(f"[DK PUBLIC] Error fetching navigation: {e}")
            return []

    def _fetch_eventgroup_odds(self, event_group_id: str, sport: str) -> List[MarketOdds]:
        """
        Fetch odds for a specific eventGroup.

        Uses the existing _parse_draftkings_json() logic from http_adapter.
        """
        url = f"{self.BASE_URL}/eventgroups/{event_group_id}?format=json"

        try:
            logger.debug(f"[DK PUBLIC] Fetching eventGroup {event_group_id}")
            response = requests.get(url, headers=self.HEADERS, timeout=10)
            response.raise_for_status()

            data = response.json()

            # Use existing DraftKings parser
            odds = self._parse_draftkings_json(data, sport)

            logger.debug(f"[DK PUBLIC] Parsed {len(odds)} odds from eventGroup {event_group_id}")
            return odds

        except Exception as e:
            logger.error(f"[DK PUBLIC] Error fetching eventGroup {event_group_id}: {e}")
            return []

    def _parse_draftkings_json(self, data: Any, sport: str) -> List[MarketOdds]:
        """
        Parse DraftKings API response.

        REUSED from http_adapter.py - DO NOT modify parsing logic.
        """
        odds_list: List[MarketOdds] = []

        try:
            # DraftKings API structure
            events = data.get("events", []) or data.get("eventGroup", {}).get("events", [])

            for event in events:
                event_id = str(event.get("eventId", ""))
                event_name = event.get("name", "")
                event_sport = self._detect_sport(event.get("eventGroupName", "")) or sport

                for market in event.get("displayGroups", [{}])[0].get("markets", []):
                    market_name = market.get("description", "moneyline")

                    for outcome in market.get("outcomes", []):
                        selection = outcome.get("description", "")
                        odds_american = outcome.get("oddsAmerican", "")
                        odds_decimal = self._american_to_decimal(odds_american)

                        if selection and odds_decimal and odds_decimal > 1.0:
                            odds_list.append(MarketOdds(
                                event_id=event_id,
                                event_name=event_name,
                                sport=event_sport,
                                market=self._normalize_market(market_name),
                                bookmaker=self.bookmaker,
                                selection=selection,
                                odds_decimal=odds_decimal,
                                captured_at=datetime.utcnow(),
                            ))
        except Exception as e:
            logger.error(f"[DK PUBLIC] Error parsing DraftKings JSON: {e}")

        return odds_list

    def _detect_sport(self, text: str) -> Optional[str]:
        """Detect sport from text."""
        if not text:
            return None
        text_lower = text.lower()
        if "nba" in text_lower or "basketball" in text_lower:
            return "nba"
        elif "nfl" in text_lower or "football" in text_lower:
            return "nfl"
        elif "mlb" in text_lower or "baseball" in text_lower:
            return "mlb"
        elif "nhl" in text_lower or "hockey" in text_lower:
            return "nhl"
        return None

    def _normalize_sport(self, sport_name: str) -> str:
        """Normalize sport name from navigation."""
        sport_lower = sport_name.lower()
        if "basketball" in sport_lower or "nba" in sport_lower:
            return "nba"
        elif "football" in sport_lower or "nfl" in sport_lower:
            return "nfl"
        elif "baseball" in sport_lower or "mlb" in sport_lower:
            return "mlb"
        elif "hockey" in sport_lower or "nhl" in sport_lower:
            return "nhl"
        return sport_lower

    def _normalize_market(self, market: str) -> str:
        """Normalize market name."""
        market_lower = market.lower()
        if "moneyline" in market_lower or "winner" in market_lower:
            return "moneyline"
        elif "spread" in market_lower or "handicap" in market_lower:
            return "spread"
        elif "total" in market_lower or "over" in market_lower:
            return "total"
        return market_lower

    def _american_to_decimal(self, american: str) -> Optional[float]:
        """Convert American odds to decimal."""
        if not american:
            return None
        try:
            american = american.replace("+", "").replace("−", "-").replace("–", "-")
            odds = int(american)
            if odds > 0:
                return 1 + (odds / 100)
            else:
                return 1 + (100 / abs(odds))
        except (ValueError, ZeroDivisionError):
            return None

