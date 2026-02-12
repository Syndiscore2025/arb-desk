"""
HTTP-based adapter that uses imported cookies for authentication.

This adapter doesn't use browser automation — it makes direct HTTP requests
with cookies exported from your browser. Much faster and more reliable than
Selenium/Playwright in Docker environments.

Usage:
1. Log into FanDuel/DraftKings in your browser
2. Export cookies (use browser extension or DevTools)
3. POST cookies to /cookies/import/{bookmaker}
4. The adapter will use those cookies for all requests
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# curl_cffi for TLS fingerprint impersonation (bypasses sportsbook API protection)
try:
    from curl_cffi import requests as curl_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    curl_requests = None

from shared.schemas import (
    BookmakerCredentials,
    FeedConfig,
    MarketOdds,
    ScrapeResult,
    SessionStatus,
)

logger = logging.getLogger(__name__)

# Cookie storage
COOKIE_DIR = Path(os.getenv("COOKIE_DIR", "/tmp/arb-desk-cookies"))


class HTTPFeedAdapter:
    """
    HTTP-based feed adapter using imported cookies.
    
    This is a lightweight alternative to browser automation that:
    - Uses cookies exported from your real browser session
    - Makes direct HTTP requests to sportsbook APIs
    - Is faster and more reliable than Selenium in Docker
    """
    
    def __init__(self, config: FeedConfig, credentials: BookmakerCredentials):
        self.config = config
        self.credentials = credentials
        self.bookmaker = config.bookmaker.lower()
        
        self.session_status = SessionStatus(
            bookmaker=self.bookmaker,
            logged_in=False,
            session_valid=False,
        )
        
        self._scrape_count = 0
        self._error_count = 0
        self._imported_cookies: List[Dict] = []
        self._client: Optional[httpx.Client] = None
    
    def _load_cookies(self) -> bool:
        """Load cookies from disk."""
        cookie_file = COOKIE_DIR / f"{self.bookmaker}.json"
        if not cookie_file.exists():
            logger.warning(f"[{self.bookmaker}] No cookie file found at {cookie_file}")
            return False
        
        try:
            with open(cookie_file, "r") as f:
                self._imported_cookies = json.load(f)
            logger.info(f"[{self.bookmaker}] Loaded {len(self._imported_cookies)} cookies")
            return True
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Failed to load cookies: {e}")
            return False
    
    def _create_client(self) -> httpx.Client:
        """Create HTTP client with cookies."""
        cookies = {}
        for c in self._imported_cookies:
            name = c.get("name")
            value = c.get("value")
            if name and value:
                cookies[name] = value

        # Headers that mimic a real browser - important for API access
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://sportsbook.fanduel.com",
            "Referer": "https://sportsbook.fanduel.com/",
        }

        return httpx.Client(
            cookies=cookies,
            headers=headers,
            timeout=30.0,
            follow_redirects=True,
        )
    
    def initialize(self) -> None:
        """Initialize the HTTP client with cookies."""
        if not self._load_cookies():
            logger.error(f"[{self.bookmaker}] Cannot initialize — no cookies")
            return
        
        self._client = self._create_client()
        self.session_status.logged_in = True
        self.session_status.session_valid = True
        self.session_status.last_login_at = datetime.utcnow()
        logger.info(f"[{self.bookmaker}] HTTP adapter initialized with cookies")
    
    def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            self._client.close()
            self._client = None
        self.session_status.logged_in = False
        self.session_status.session_valid = False
    
    def login(self) -> bool:
        """
        'Login' by loading cookies.
        
        This doesn't actually log in — it just loads previously exported cookies.
        """
        if self._load_cookies():
            self._client = self._create_client()
            self.session_status.logged_in = True
            self.session_status.session_valid = True
            self.session_status.last_login_at = datetime.utcnow()
            return True
        return False
    
    def scrape(self) -> ScrapeResult:
        """Scrape odds using HTTP requests."""
        if not self._client:
            if not self.login():
                return ScrapeResult(
                    bookmaker=self.bookmaker,
                    success=False,
                    error="No cookies available. Import via /cookies/import/{bookmaker}",
                )
        
        self._scrape_count += 1
        odds_list: List[MarketOdds] = []
        
        try:
            # Scrape each configured URL
            for url in self.config.odds_urls or []:
                page_odds = self._scrape_url(url)
                odds_list.extend(page_odds)
            
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=True,
                odds=odds_list,
                scraped_at=datetime.utcnow(),
            )
        except Exception as e:
            self._error_count += 1
            logger.error(f"[{self.bookmaker}] Scrape error: {e}")
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=False,
                error=str(e),
            )

    def _scrape_url(self, url: str) -> List[MarketOdds]:
        """Scrape odds from a single URL."""
        odds_list: List[MarketOdds] = []

        try:
            # Use curl_cffi for FanDuel API endpoints (bypasses TLS fingerprinting)
            if "sbapi.fanduel.com" in url and CURL_CFFI_AVAILABLE:
                return self._scrape_with_curl_cffi(url)

            response = self._client.get(url)

            if response.status_code == 401 or response.status_code == 403:
                logger.warning(f"[{self.bookmaker}] Session expired (HTTP {response.status_code})")
                self.session_status.session_valid = False
                return odds_list

            response.raise_for_status()

            # Try to parse as JSON (API response)
            content_type = response.headers.get("content-type", "")
            logger.debug(f"[{self.bookmaker}] Response content-type: {content_type}, length: {len(response.text)}")

            if "application/json" in content_type:
                data = response.json()
                logger.info(f"[{self.bookmaker}] Received JSON response from {url}")
                odds_list = self._parse_json_odds(data, url)
            else:
                # HTML page — extract odds from HTML
                html_text = response.text
                # Log a snippet of the response to help debug
                if "__NEXT_DATA__" in html_text:
                    logger.info(f"[{self.bookmaker}] HTML contains __NEXT_DATA__ tag")
                else:
                    logger.warning(f"[{self.bookmaker}] HTML does NOT contain __NEXT_DATA__ tag - first 500 chars: {html_text[:500]}")
                odds_list = self._parse_html_odds(html_text, url)

            logger.info(f"[{self.bookmaker}] Scraped {len(odds_list)} odds from {url}")

        except httpx.HTTPStatusError as e:
            logger.error(f"[{self.bookmaker}] HTTP error for {url}: {e}")
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error scraping {url}: {e}")

        return odds_list

    def _scrape_with_curl_cffi(self, url: str) -> List[MarketOdds]:
        """Use curl_cffi to bypass TLS fingerprinting for FanDuel API."""
        odds_list: List[MarketOdds] = []

        try:
            # Build cookies dict
            cookies = {}
            for c in self._imported_cookies:
                name = c.get("name")
                value = c.get("value")
                if name and value:
                    cookies[name] = value

            # Headers that mimic Chrome browser
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://sportsbook.fanduel.com",
                "Referer": "https://sportsbook.fanduel.com/",
            }

            logger.info(f"[{self.bookmaker}] Using curl_cffi for {url}")

            # Use curl_cffi with Chrome impersonation to bypass TLS fingerprinting
            response = curl_requests.get(
                url,
                headers=headers,
                cookies=cookies,
                impersonate="chrome120",
                timeout=30,
            )

            if response.status_code == 401 or response.status_code == 403:
                logger.warning(f"[{self.bookmaker}] Session expired (HTTP {response.status_code})")
                self.session_status.session_valid = False
                return odds_list

            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            logger.info(f"[{self.bookmaker}] curl_cffi response: status={response.status_code}, content-type={content_type}")

            if "application/json" in content_type:
                data = response.json()
                logger.info(f"[{self.bookmaker}] Received JSON from FanDuel API")
                odds_list = self._parse_fanduel_json(data)
            else:
                logger.warning(f"[{self.bookmaker}] FanDuel API returned non-JSON: {response.text[:500]}")

            logger.info(f"[{self.bookmaker}] Scraped {len(odds_list)} odds from {url}")

        except Exception as e:
            logger.error(f"[{self.bookmaker}] curl_cffi error for {url}: {e}")

        return odds_list

    def _parse_json_odds(self, data: Any, url: str) -> List[MarketOdds]:
        """Parse odds from JSON API response."""
        odds_list: List[MarketOdds] = []

        # Detect bookmaker and use appropriate parser
        if self.bookmaker == "fanduel":
            odds_list = self._parse_fanduel_json(data)
        elif self.bookmaker == "draftkings":
            odds_list = self._parse_draftkings_json(data)
        else:
            logger.warning(f"[{self.bookmaker}] No JSON parser for this bookmaker")

        return odds_list

    def _parse_html_odds(self, html: str, url: str) -> List[MarketOdds]:
        """Parse odds from HTML page.

        FanDuel and DraftKings use Next.js and embed JSON data in a <script id="__NEXT_DATA__"> tag.
        We extract and parse that JSON.
        """
        import re

        # Try to extract __NEXT_DATA__ JSON from the page
        match = re.search(r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if match:
            try:
                next_data = json.loads(match.group(1))
                logger.info(f"[{self.bookmaker}] Found __NEXT_DATA__ in HTML, parsing...")

                # Navigate to the page props where odds data lives
                page_props = next_data.get("props", {}).get("pageProps", {})

                if self.bookmaker == "fanduel":
                    return self._parse_fanduel_next_data(page_props)
                elif self.bookmaker == "draftkings":
                    return self._parse_draftkings_next_data(page_props)

            except json.JSONDecodeError as e:
                logger.error(f"[{self.bookmaker}] Failed to parse __NEXT_DATA__: {e}")
        else:
            logger.debug(f"[{self.bookmaker}] No __NEXT_DATA__ found in HTML for {url}")

        return []

    def _parse_fanduel_next_data(self, page_props: Dict) -> List[MarketOdds]:
        """Parse FanDuel odds from __NEXT_DATA__ pageProps."""
        odds_list: List[MarketOdds] = []

        try:
            # FanDuel structure: pageProps -> initialState -> eventDisplay -> events
            initial_state = page_props.get("initialState", {})

            # Try different possible structures
            attachments = initial_state.get("attachments", {})
            events = attachments.get("events", {})
            markets = attachments.get("markets", {})

            if not events:
                # Alternative structure: search for events in different paths
                events = initial_state.get("events", {})

            if not events:
                logger.debug(f"[{self.bookmaker}] No events found in __NEXT_DATA__")
                return odds_list

            logger.info(f"[{self.bookmaker}] Found {len(events)} events in __NEXT_DATA__")

            for event_id, event in events.items():
                event_name = event.get("name", "")
                sport = self._detect_sport(event.get("competitionName", "") or event.get("competition", {}).get("name", ""))

                event_markets = event.get("markets", [])
                for market_id in event_markets:
                    market = markets.get(str(market_id), {})
                    market_name = market.get("marketName", "") or market.get("marketType", "moneyline")

                    for runner in market.get("runners", []):
                        selection = runner.get("runnerName", "")
                        odds_info = runner.get("winRunnerOdds", {})
                        odds_decimal = odds_info.get("decimal") if odds_info else None

                        # Also try americanDisplayOdds
                        if not odds_decimal:
                            american = odds_info.get("americanDisplayOdds", {}).get("americanOdds") if odds_info else None
                            if american:
                                odds_decimal = self._american_to_decimal(str(american))

                        if selection and odds_decimal and float(odds_decimal) > 1.0:
                            odds_list.append(MarketOdds(
                                event_id=str(event_id),
                                event_name=event_name,
                                sport=sport,
                                market=self._normalize_market(market_name),
                                bookmaker=self.bookmaker,
                                selection=selection,
                                odds_decimal=float(odds_decimal),
                                captured_at=datetime.utcnow(),
                            ))

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error parsing FanDuel __NEXT_DATA__: {e}")

        return odds_list

    def _parse_draftkings_next_data(self, page_props: Dict) -> List[MarketOdds]:
        """Parse DraftKings odds from __NEXT_DATA__ pageProps."""
        odds_list: List[MarketOdds] = []

        try:
            # DraftKings structure varies - try common patterns
            initial_data = page_props.get("initialData", {})

            events = (
                initial_data.get("events", []) or
                page_props.get("events", []) or
                initial_data.get("eventGroup", {}).get("events", [])
            )

            if not events:
                logger.debug(f"[{self.bookmaker}] No events found in __NEXT_DATA__")
                return odds_list

            logger.info(f"[{self.bookmaker}] Found {len(events)} events in __NEXT_DATA__")

            for event in events:
                event_id = str(event.get("eventId", ""))
                event_name = event.get("name", "")
                sport = self._detect_sport(event.get("eventGroupName", ""))

                display_groups = event.get("displayGroups", [])
                if display_groups:
                    for market in display_groups[0].get("markets", []):
                        market_name = market.get("description", "moneyline")

                        for outcome in market.get("outcomes", []):
                            selection = outcome.get("description", "")
                            odds_american = outcome.get("oddsAmerican", "")
                            odds_decimal = self._american_to_decimal(odds_american)

                            if selection and odds_decimal and odds_decimal > 1.0:
                                odds_list.append(MarketOdds(
                                    event_id=event_id,
                                    event_name=event_name,
                                    sport=sport,
                                    market=self._normalize_market(market_name),
                                    bookmaker=self.bookmaker,
                                    selection=selection,
                                    odds_decimal=odds_decimal,
                                    captured_at=datetime.utcnow(),
                                ))

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error parsing DraftKings __NEXT_DATA__: {e}")

        return odds_list

    def _parse_fanduel_json(self, data: Any) -> List[MarketOdds]:
        """Parse FanDuel API response."""
        odds_list: List[MarketOdds] = []

        try:
            # Log the top-level keys to understand the structure
            if isinstance(data, dict):
                logger.info(f"[{self.bookmaker}] JSON top-level keys: {list(data.keys())[:10]}")
            else:
                logger.warning(f"[{self.bookmaker}] JSON is not a dict, type: {type(data)}")
                return odds_list

            # FanDuel API structure varies, try common patterns
            events = data.get("attachments", {}).get("events", {})
            markets = data.get("attachments", {}).get("markets", {})

            # Log what we found
            logger.info(f"[{self.bookmaker}] Found {len(events)} events, {len(markets)} markets in attachments")

            for event_id, event in events.items():
                event_name = event.get("name", "")
                sport = self._detect_sport(event.get("competitionName", ""))

                for market_id in event.get("markets", []):
                    market = markets.get(str(market_id), {})
                    market_name = market.get("marketName", "moneyline")

                    for runner in market.get("runners", []):
                        selection = runner.get("runnerName", "")
                        odds_decimal = runner.get("winRunnerOdds", {}).get("decimal")

                        if selection and odds_decimal and float(odds_decimal) > 1.0:
                            odds_list.append(MarketOdds(
                                event_id=str(event_id),
                                event_name=event_name,
                                sport=sport,
                                market=self._normalize_market(market_name),
                                bookmaker=self.bookmaker,
                                selection=selection,
                                odds_decimal=float(odds_decimal),
                                captured_at=datetime.utcnow(),
                            ))
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error parsing FanDuel JSON: {e}")

        return odds_list

    def _parse_draftkings_json(self, data: Any) -> List[MarketOdds]:
        """Parse DraftKings API response."""
        odds_list: List[MarketOdds] = []

        try:
            # DraftKings API structure
            events = data.get("events", []) or data.get("eventGroup", {}).get("events", [])

            for event in events:
                event_id = str(event.get("eventId", ""))
                event_name = event.get("name", "")
                sport = self._detect_sport(event.get("eventGroupName", ""))

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
                                sport=sport,
                                market=self._normalize_market(market_name),
                                bookmaker=self.bookmaker,
                                selection=selection,
                                odds_decimal=odds_decimal,
                                captured_at=datetime.utcnow(),
                            ))
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error parsing DraftKings JSON: {e}")

        return odds_list

    def _detect_sport(self, text: str) -> str:
        """Detect sport from text."""
        text_lower = text.lower()
        if "nba" in text_lower or "basketball" in text_lower:
            return "nba"
        elif "nfl" in text_lower or "football" in text_lower:
            return "nfl"
        elif "mlb" in text_lower or "baseball" in text_lower:
            return "mlb"
        elif "nhl" in text_lower or "hockey" in text_lower:
            return "nhl"
        return "unknown"

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

