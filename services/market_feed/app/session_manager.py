"""
Session manager for handling browser sessions across multiple sportsbooks.
Manages login, session persistence, and auto-relogin on expiration.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from shared.schemas import BookmakerCredentials, FeedConfig, FeedStatus, SessionStatus
from .adapters.base import BaseFeedAdapter
from .adapters.generic import GenericSportsbookAdapter

logger = logging.getLogger(__name__)
browser_logger = logging.getLogger("market_feed.browser.session")


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
        """Create a new adapter instance."""
        config = self._configs[bookmaker]
        credentials = self._credentials[bookmaker]
        
        # For now, use the generic adapter
        # In the future, could use a factory pattern for specific adapters
        return GenericSportsbookAdapter(config, credentials)
    
    def ensure_logged_in(self, bookmaker: str) -> bool:
        """
        Ensure the adapter is logged in, attempting login if needed.
        Returns True if logged in, False otherwise.
        """
        adapter = self.get_adapter(bookmaker)
        if not adapter:
            browser_logger.warning(
                f"No adapter for {bookmaker}",
                extra={"event_type": "login_no_adapter", "bookmaker": bookmaker}
            )
            return False

        # Already logged in and session valid
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

        # Attempt login
        browser_logger.info(
            f"Attempting login for {bookmaker}",
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
                f"Login failed for {bookmaker}",
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

