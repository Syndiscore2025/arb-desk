"""
Session manager for handling browser sessions across multiple sportsbooks.
Manages login, session persistence, and auto-relogin on expiration.

Supports two modes:
1. Cookie-based: User logs in manually, exports cookies, imports via API
2. Browser automation: Selenium/Playwright (currently broken on Windows Docker)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from shared.schemas import BookmakerCredentials, FeedConfig, FeedStatus, SessionStatus
from .adapters.base import BaseFeedAdapter
from .adapters.generic import GenericSportsbookAdapter
from .adapters.http_adapter import HTTPFeedAdapter
from .adapters.intercepting_adapter import InterceptingAdapter

logger = logging.getLogger(__name__)
browser_logger = logging.getLogger("market_feed.browser.session")

# Cookie storage directory
COOKIE_DIR = Path(os.getenv("COOKIE_DIR", "/tmp/arb-desk-cookies"))
COOKIE_DIR.mkdir(parents=True, exist_ok=True)


class SessionManager:
    """
    Manages browser sessions for multiple sportsbook adapters.
    
    Features:
    - Lazy initialization of adapters
    - Auto-relogin on session expiration
    - Rate limiting for login attempts
    - Thread-safe session access
    """
    
    # Minimum time between login attempts (to avoid rate limiting)
    MIN_LOGIN_INTERVAL = timedelta(minutes=1)
    MAX_LOGIN_FAILURES = 5
    
    def __init__(self):
        self._adapters: Dict[str, BaseFeedAdapter] = {}
        self._configs: Dict[str, FeedConfig] = {}
        self._credentials: Dict[str, BookmakerCredentials] = {}
        self._last_login_attempt: Dict[str, datetime] = {}
        self._lock = threading.Lock()
    
    def register_feed(
        self,
        config: FeedConfig,
        credentials: BookmakerCredentials,
    ) -> None:
        """Register a new feed configuration."""
        with self._lock:
            bookmaker = config.bookmaker
            self._configs[bookmaker] = config
            self._credentials[bookmaker] = credentials
            logger.info(f"Registered feed for {bookmaker}")
    
    def unregister_feed(self, bookmaker: str) -> None:
        """Unregister and close a feed."""
        with self._lock:
            if bookmaker in self._adapters:
                self._adapters[bookmaker].close()
                del self._adapters[bookmaker]
            self._configs.pop(bookmaker, None)
            self._credentials.pop(bookmaker, None)
            self._last_login_attempt.pop(bookmaker, None)
            logger.info(f"Unregistered feed for {bookmaker}")
    
    def get_adapter(self, bookmaker: str) -> Optional[BaseFeedAdapter]:
        """Get or create an adapter for a bookmaker."""
        with self._lock:
            if bookmaker not in self._configs:
                logger.warning(f"No config registered for {bookmaker}")
                return None
            
            if bookmaker not in self._adapters:
                self._adapters[bookmaker] = self._create_adapter(bookmaker)
            
            return self._adapters[bookmaker]
    
    def _create_adapter(self, bookmaker: str) -> BaseFeedAdapter:
        """Create a new adapter instance.

        Priority:
        1. HTTPFeedAdapter when cookies are available (fastest, most reliable)
        2. InterceptingAdapter for CT sportsbooks (Playwright + API interception)
        3. GenericSportsbookAdapter fallback (Selenium, legacy)
        """
        config = self._configs[bookmaker]
        credentials = self._credentials[bookmaker]

        # Prefer HTTP adapter when cookies are available
        if self._has_imported_cookies(bookmaker):
            logger.info(f"[{bookmaker}] Using HTTPFeedAdapter (cookies available)")
            adapter = HTTPFeedAdapter(config, credentials)
            adapter.initialize()
            return adapter

        # Use InterceptingAdapter for CT sportsbooks (Playwright + network interception)
        # This is the recommended approach for live odds scraping
        ct_books = ["fanduel", "draftkings", "fanatics"]
        if bookmaker.lower() in ct_books:
            logger.info(f"[{bookmaker}] Using InterceptingAdapter (Playwright + API interception)")
            # InterceptingAdapter is async, but we return it here for lazy init
            # The actual browser initialization happens when login() is called
            return InterceptingAdapter(config, credentials)

        # Fall back to Selenium adapter (may not work in Docker)
        logger.info(f"[{bookmaker}] Using GenericSportsbookAdapter (no cookies)")
        return GenericSportsbookAdapter(config, credentials)
    
    def _has_imported_cookies(self, bookmaker: str) -> bool:
        """Check if we have imported cookies for this bookmaker."""
        cookie_file = COOKIE_DIR / f"{bookmaker.lower()}.json"
        return cookie_file.exists()

    def _load_imported_cookies(self, bookmaker: str) -> Optional[List[Dict]]:
        """Load imported cookies from disk."""
        cookie_file = COOKIE_DIR / f"{bookmaker.lower()}.json"
        if not cookie_file.exists():
            return None
        try:
            with open(cookie_file, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[{bookmaker}] Failed to load cookies: {e}")
            return None

    def ensure_logged_in(self, bookmaker: str) -> bool:
        """
        Ensure the adapter is logged in, attempting login if needed.
        Returns True if logged in, False otherwise.

        Priority:
        1. Check if session is already valid
        2. Check for imported cookies (cookie-based auth)
        3. Fall back to browser automation (may fail on Windows Docker)
        """
        adapter = self.get_adapter(bookmaker)
        if not adapter:
            browser_logger.warning(
                f"No adapter for {bookmaker}",
                extra={"event_type": "login_no_adapter", "bookmaker": bookmaker}
            )
            return False

        # Check for imported cookies — if present, ensure we're using HTTP adapter
        # This check comes FIRST because we may need to switch adapter types
        if self._has_imported_cookies(bookmaker):
            cookies = self._load_imported_cookies(bookmaker)
            if cookies:
                # If current adapter is not HTTPFeedAdapter, recreate it
                if not isinstance(adapter, HTTPFeedAdapter):
                    browser_logger.info(
                        f"Switching {bookmaker} to HTTPFeedAdapter (cookies imported)",
                        extra={"event_type": "adapter_switch", "bookmaker": bookmaker}
                    )
                    with self._lock:
                        # Close old adapter
                        if bookmaker in self._adapters:
                            self._adapters[bookmaker].close()
                        # Create new HTTP adapter
                        self._adapters[bookmaker] = self._create_adapter(bookmaker)
                        adapter = self._adapters[bookmaker]
                elif not adapter.session_status.session_valid:
                    browser_logger.info(
                        f"Using imported cookies for {bookmaker} ({len(cookies)} cookies)",
                        extra={"event_type": "cookie_auth", "bookmaker": bookmaker}
                    )

                adapter.session_status.logged_in = True
                adapter.session_status.session_valid = True
                adapter.session_status.last_login_at = datetime.utcnow()
                adapter.session_status.login_failures = 0
                adapter.session_status.error = None
                return True

        # Already logged in and session valid (no cookies available)
        if adapter.session_status.session_valid:
            browser_logger.debug(
                f"Session valid for {bookmaker}",
                extra={"event_type": "session_valid", "bookmaker": bookmaker}
            )
            return True

        # Check if we can attempt login (rate limiting)
        if not self._can_attempt_login(bookmaker):
            browser_logger.warning(
                f"Login rate limited for {bookmaker}",
                extra={"event_type": "login_rate_limited", "bookmaker": bookmaker}
            )
            return False

        # Check if too many failures
        if adapter.session_status.login_failures >= self.MAX_LOGIN_FAILURES:
            browser_logger.error(
                f"Too many login failures for {bookmaker}",
                extra={
                    "event_type": "login_max_failures",
                    "bookmaker": bookmaker,
                    "failures": adapter.session_status.login_failures
                }
            )
            return False

        # Attempt browser-based login (may fail on Windows Docker)
        browser_logger.info(
            f"Attempting browser login for {bookmaker}",
            extra={"event_type": "login_attempt", "bookmaker": bookmaker}
        )
        self._last_login_attempt[bookmaker] = datetime.utcnow()

        start_time = time.time()
        success = adapter.login()
        duration_ms = int((time.time() - start_time) * 1000)

        if success:
            browser_logger.info(
                f"Login successful for {bookmaker}",
                extra={
                    "event_type": "login_success",
                    "bookmaker": bookmaker,
                    "duration_ms": duration_ms
                }
            )
        else:
            browser_logger.error(
                f"Login failed for {bookmaker}. Import cookies via /cookies/import/{bookmaker}",
                extra={
                    "event_type": "login_failed",
                    "bookmaker": bookmaker,
                    "duration_ms": duration_ms,
                    "failures": adapter.session_status.login_failures
                }
            )

        return success
    
    def _can_attempt_login(self, bookmaker: str) -> bool:
        """Check if enough time has passed since last login attempt."""
        last_attempt = self._last_login_attempt.get(bookmaker)
        if not last_attempt:
            return True
        
        elapsed = datetime.utcnow() - last_attempt
        return elapsed >= self.MIN_LOGIN_INTERVAL
    
    def get_status(self, bookmaker: str) -> Optional[FeedStatus]:
        """Get the current status of a feed."""
        config = self._configs.get(bookmaker)
        if not config:
            return None
        
        adapter = self._adapters.get(bookmaker)
        
        if adapter:
            session = adapter.session_status
            return FeedStatus(
                bookmaker=bookmaker,
                enabled=config.enabled,
                running=adapter.session_status.session_valid,
                session=session,
                scrape_count=adapter._scrape_count,
                error_count=adapter._error_count,
            )
        else:
            return FeedStatus(
                bookmaker=bookmaker,
                enabled=config.enabled,
                running=False,
                session=SessionStatus(bookmaker=bookmaker),
            )
    
    def get_all_status(self) -> Dict[str, FeedStatus]:
        """Get status for all registered feeds."""
        return {bm: self.get_status(bm) for bm in self._configs}
    
    def close_all(self) -> None:
        """Close all adapter sessions."""
        with self._lock:
            for adapter in self._adapters.values():
                adapter.close()
            self._adapters.clear()
            logger.info("Closed all adapter sessions")


# Global session manager instance
session_manager = SessionManager()

