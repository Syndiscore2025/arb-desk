"""
API Interception Adapter for Real-Time Live Odds.

Instead of scraping the DOM with CSS selectors (which breaks constantly),
this adapter intercepts the network requests that the sportsbook's frontend
makes to its internal API. This gives us:

1. Structured JSON data directly from the source
2. No CSS selector guessing or maintenance
3. Real-time data (not delayed like third-party APIs)
4. Works even when the UI changes (API structure is more stable)

Requires:
- Login to sportsbook (uses browser session)
- 2FA support (Slack-based or TOTP)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from shared.schemas import MarketOdds
from .playwright_generic import PlaywrightGenericAdapter
from ..stealth_playwright import async_jittered_delay

logger = logging.getLogger(__name__)


# API endpoint patterns to intercept
FANDUEL_API_PATTERNS = [
    r"sbapi\.fanduel\.com.*events",
    r"sbapi\.fanduel\.com.*markets",
    r"sbapi\.fanduel\.com.*odds",
]

DRAFTKINGS_API_PATTERNS = [
    r"api\.draftkings\.com.*sportscontent",
    r"sportsbook-us-.*\.draftkings\.com",
    r"api\.draftkings\.com.*offers",
]

FANATICS_API_PATTERNS = [
    r"api\.fanatics\.sportsbook",
    r"fanatics\.api",
]


class InterceptingAdapter(PlaywrightGenericAdapter):
    """
    Playwright adapter that intercepts network requests to capture odds data.
    
    Instead of scraping the DOM with CSS selectors, this adapter:
    1. Opens the real sportsbook page with login
    2. Intercepts XHR/fetch requests to the sportsbook's internal API
    3. Captures the JSON responses directly
    4. Parses structured data without CSS selectors
    
    This approach is far more reliable than CSS scraping because:
    - API structures change less frequently than UI
    - We get structured JSON, not HTML to parse
    - Works regardless of UI framework (React, Vue, etc.)
    - Gets the same data the sportsbook's own frontend uses
    """
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._intercepted_data: List[Dict[str, Any]] = []
        self._interception_active = False
        self._api_patterns: List[str] = []
        
        # Set up API patterns based on bookmaker
        if "fanduel" in self.bookmaker.lower():
            self._api_patterns = FANDUEL_API_PATTERNS
        elif "draftkings" in self.bookmaker.lower():
            self._api_patterns = DRAFTKINGS_API_PATTERNS
        elif "fanatics" in self.bookmaker.lower():
            self._api_patterns = FANATICS_API_PATTERNS
        else:
            logger.warning(f"[{self.bookmaker}] No API patterns defined, using generic")
            self._api_patterns = [r"api.*odds", r"api.*events", r"api.*markets"]
    
    async def _setup_interception(self) -> None:
        """Set up network request interception."""
        if not self.browser or not self.browser.page:
            raise RuntimeError("Browser not initialized")
        
        self._intercepted_data = []
        self._interception_active = True
        
        async def handle_response(response):
            """Capture API responses."""
            if not self._interception_active:
                return
            
            url = response.url
            
            # Check if URL matches any of our patterns
            for pattern in self._api_patterns:
                if re.search(pattern, url, re.IGNORECASE):
                    try:
                        content_type = response.headers.get("content-type", "")
                        if "json" in content_type:
                            body = await response.json()
                            self._intercepted_data.append({
                                "url": url,
                                "data": body,
                                "timestamp": datetime.utcnow().isoformat(),
                            })
                            logger.debug(
                                f"[{self.bookmaker}] Intercepted API response: "
                                f"{url[:80]}... ({len(json.dumps(body))} bytes)"
                            )
                    except Exception as e:
                        logger.debug(f"[{self.bookmaker}] Failed to parse response: {e}")
                    break
        
        # Register response handler
        self.browser.page.on("response", handle_response)
        logger.info(f"[{self.bookmaker}] API interception enabled")
    
    def _stop_interception(self) -> None:
        """Stop intercepting requests."""
        self._interception_active = False
    
    async def _scrape_odds(self) -> List[MarketOdds]:
        """
        Scrape odds by intercepting API responses.
        
        1. Set up network interception
        2. Navigate to odds pages (triggers API calls)
        3. Collect and parse intercepted data
        """
        # Set up interception
        await self._setup_interception()
        
        all_odds: List[MarketOdds] = []
        
        try:
            # Navigate to each odds URL to trigger API calls
            for url in self.config.odds_urls:
                logger.info(f"[{self.bookmaker}] Navigating to {url}")
                
                # Clear previous intercepted data
                self._intercepted_data = []
                
                await self.browser.page.goto(url, wait_until="networkidle")
                await async_jittered_delay(2, 4)

                # Scroll to trigger lazy-loaded content
                await self.browser.human_scroll(500)
                await async_jittered_delay(1, 2)

                # Parse intercepted data
                for capture in self._intercepted_data:
                    parsed = self._parse_intercepted_data(capture["data"])
                    all_odds.extend(parsed)

            # Handle live URLs separately
            if self.config.live_odds_urls:
                for url in self.config.live_odds_urls:
                    logger.info(f"[{self.bookmaker}] Navigating to live: {url}")
                    self._intercepted_data = []

                    await self.browser.page.goto(url, wait_until="networkidle")
                    await async_jittered_delay(3, 5)

                    # Parse live data
                    for capture in self._intercepted_data:
                        parsed = self._parse_intercepted_data(capture["data"], is_live=True)
                        all_odds.extend(parsed)

            logger.info(f"[{self.bookmaker}] Intercepted {len(all_odds)} total odds")
            return all_odds

        finally:
            self._stop_interception()

    def _parse_intercepted_data(
        self, data: Any, is_live: bool = False
    ) -> List[MarketOdds]:
        """
        Parse intercepted JSON data into MarketOdds.

        Routes to book-specific parsers based on data structure.
        """
        if not isinstance(data, dict):
            return []

        # Detect and route to correct parser
        if "fanduel" in self.bookmaker.lower():
            return self._parse_fanduel_data(data, is_live)
        elif "draftkings" in self.bookmaker.lower():
            return self._parse_draftkings_data(data, is_live)
        elif "fanatics" in self.bookmaker.lower():
            return self._parse_fanatics_data(data, is_live)
        else:
            return self._parse_generic_data(data, is_live)

    def _parse_fanduel_data(self, data: dict, is_live: bool) -> List[MarketOdds]:
        """Parse FanDuel API response."""
        odds_list = []

        try:
            # FanDuel uses "attachments" structure
            attachments = data.get("attachments", {})
            events = attachments.get("events", {})
            markets = attachments.get("markets", {})

            for event_id, event in events.items():
                event_name = event.get("name", f"Event {event_id}")
                sport = self._extract_sport(event)

                # Get markets for this event
                event_markets = event.get("markets", [])
                for market_id in event_markets:
                    market = markets.get(str(market_id), {})
                    market_name = market.get("marketName", "Unknown")
                    market_type = self._normalize_market_type(market_name)

                    # Get selections/runners
                    runners = market.get("runners", [])
                    for runner in runners:
                        selection_name = runner.get("runnerName", "Unknown")

                        # Get price
                        win_price = runner.get("winRunnerOdds", {})
                        decimal_odds = win_price.get("decimal", 0)
                        american_odds = win_price.get("american")

                        if decimal_odds and decimal_odds > 1:
                            odds_list.append(MarketOdds(
                                event_id=str(event_id),
                                sport=sport,
                                market=market_name,
                                bookmaker="fanduel",
                                selection=selection_name,
                                odds_decimal=decimal_odds,
                                market_type=market_type,
                                is_live=is_live,
                                line=runner.get("handicap"),
                            ))
        except Exception as e:
            logger.error(f"[{self.bookmaker}] FanDuel parse error: {e}")

        return odds_list

    def _parse_draftkings_data(self, data: dict, is_live: bool) -> List[MarketOdds]:
        """Parse DraftKings API response."""
        odds_list = []

        try:
            # DraftKings uses "events" or "eventGroup" structure
            events = data.get("events", data.get("eventGroup", {}).get("events", []))

            if isinstance(events, dict):
                events = list(events.values())

            for event in events:
                event_id = str(event.get("eventId", event.get("id", "")))
                event_name = event.get("name", f"Event {event_id}")
                sport = self._extract_sport(event)

                # Get offer categories (markets)
                offer_categories = event.get("offerCategories", [])
                for category in offer_categories:
                    offers = category.get("offerSubcategoryDescriptors", [])
                    for sub in offers:
                        market_offers = sub.get("offers", [[]])
                        for offer_group in market_offers:
                            for offer in offer_group:
                                market_name = offer.get("label", "Unknown")
                                market_type = self._normalize_market_type(market_name)

                                outcomes = offer.get("outcomes", [])
                                for outcome in outcomes:
                                    selection = outcome.get("label", "Unknown")
                                    decimal_odds = outcome.get("oddsDecimal", 0)

                                    if not decimal_odds:
                                        american = outcome.get("oddsAmerican")
                                        if american:
                                            decimal_odds = self._american_to_decimal(american)

                                    if decimal_odds and decimal_odds > 1:
                                        odds_list.append(MarketOdds(
                                            event_id=event_id,
                                            sport=sport,
                                            market=market_name,
                                            bookmaker="draftkings",
                                            selection=selection,
                                            odds_decimal=decimal_odds,
                                            market_type=market_type,
                                            is_live=is_live,
                                            line=outcome.get("line"),
                                        ))
        except Exception as e:
            logger.error(f"[{self.bookmaker}] DraftKings parse error: {e}")

        return odds_list

    def _parse_fanatics_data(self, data: dict, is_live: bool) -> List[MarketOdds]:
        """Parse Fanatics API response."""
        odds_list = []

        try:
            # Fanatics API structure (similar patterns to DraftKings)
            events = data.get("events", [])

            for event in events:
                event_id = str(event.get("id", ""))
                sport = self._extract_sport(event)

                markets = event.get("markets", [])
                for market in markets:
                    market_name = market.get("name", "Unknown")
                    market_type = self._normalize_market_type(market_name)

                    selections = market.get("selections", market.get("outcomes", []))
                    for sel in selections:
                        selection_name = sel.get("name", sel.get("label", "Unknown"))
                        decimal_odds = sel.get("odds", sel.get("decimalOdds", 0))

                        if decimal_odds and decimal_odds > 1:
                            odds_list.append(MarketOdds(
                                event_id=event_id,
                                sport=sport,
                                market=market_name,
                                bookmaker="fanatics",
                                selection=selection_name,
                                odds_decimal=decimal_odds,
                                market_type=market_type,
                                is_live=is_live,
                                line=sel.get("line", sel.get("handicap")),
                            ))
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Fanatics parse error: {e}")

        return odds_list

    def _parse_generic_data(self, data: dict, is_live: bool) -> List[MarketOdds]:
        """Generic fallback parser for unknown data structures."""
        odds_list = []

        # Try common patterns
        events = data.get("events", data.get("data", data.get("results", [])))
        if isinstance(events, dict):
            events = list(events.values())

        for event in events:
            if not isinstance(event, dict):
                continue

            event_id = str(event.get("id", event.get("eventId", "")))
            sport = self._extract_sport(event)

            # Look for markets/outcomes in various structures
            markets = event.get("markets", event.get("offers", []))
            for market in markets:
                if not isinstance(market, dict):
                    continue

                market_name = market.get("name", market.get("label", "Unknown"))
                selections = market.get("selections", market.get("outcomes", []))

                for sel in selections:
                    if not isinstance(sel, dict):
                        continue

                    selection_name = sel.get("name", sel.get("label", "Unknown"))
                    decimal_odds = (
                        sel.get("odds") or
                        sel.get("decimalOdds") or
                        sel.get("price", {}).get("decimal", 0)
                    )

                    if decimal_odds and decimal_odds > 1:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market=market_name,
                            bookmaker=self.bookmaker,
                            selection=selection_name,
                            odds_decimal=decimal_odds,
                            is_live=is_live,
                        ))

        return odds_list

    def _extract_sport(self, event: dict) -> str:
        """Extract sport name from event data."""
        sport = (
            event.get("sport") or
            event.get("sportName") or
            event.get("competition", {}).get("sport", {}).get("name") or
            event.get("league", {}).get("sport") or
            "unknown"
        )
        return str(sport).lower()

    def _normalize_market_type(self, market_name: str) -> str:
        """Normalize market name to standard type."""
        name_lower = market_name.lower()

        if any(x in name_lower for x in ["moneyline", "money line", "match winner", "to win"]):
            return "moneyline"
        elif any(x in name_lower for x in ["spread", "handicap", "point spread"]):
            return "spread"
        elif any(x in name_lower for x in ["total", "over/under", "o/u"]):
            return "total"
        elif any(x in name_lower for x in ["prop", "player"]):
            return "prop"
        elif any(x in name_lower for x in ["future", "outright", "championship"]):
            return "future"
        else:
            return "other"

    def _american_to_decimal(self, american: int) -> float:
        """Convert American odds to decimal."""
        if american >= 100:
            return round(1 + (american / 100), 3)
        else:
            return round(1 + (100 / abs(american)), 3)

