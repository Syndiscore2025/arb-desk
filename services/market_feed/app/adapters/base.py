"""
Base adapter class for sportsbook feed scrapers.
Defines the interface that all sportsbook-specific adapters must implement.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from selenium.webdriver.remote.webdriver import WebDriver

from shared.schemas import (
    BookmakerCredentials,
    FeedConfig,
    MarketOdds,
    ScrapeResult,
    SessionStatus,
)
from ..stealth import create_stealth_driver, jittered_delay

logger = logging.getLogger(__name__)


class BaseFeedAdapter(ABC):
    """
    Abstract base class for sportsbook feed adapters.
    
    Each sportsbook requires a specific adapter that knows how to:
    1. Log in with credentials
    2. Navigate to odds pages
    3. Extract and normalize odds data
    4. Handle session management
    """
    
    def __init__(
        self,
        config: FeedConfig,
        credentials: BookmakerCredentials,
    ):
        self.config = config
        self.credentials = credentials
        self.driver: Optional[WebDriver] = None
        self.session_status = SessionStatus(
            bookmaker=config.bookmaker,
            logged_in=False,
            session_valid=False,
        )
        self._scrape_count = 0
        self._error_count = 0
    
    @property
    def bookmaker(self) -> str:
        return self.config.bookmaker
    
    def initialize(self) -> None:
        """Initialize the browser driver."""
        if self.driver is not None:
            self.close()
        
        logger.info(f"[{self.bookmaker}] Initializing stealth browser...")
        self.driver = create_stealth_driver(
            headless=self.config.headless,
            proxy=self.config.proxy,
        )
        logger.info(f"[{self.bookmaker}] Browser initialized")
    
    def close(self) -> None:
        """Close the browser driver."""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                logger.warning(f"[{self.bookmaker}] Error closing driver: {e}")
            finally:
                self.driver = None
                self.session_status.logged_in = False
                self.session_status.session_valid = False
    
    def login(self) -> bool:
        """
        Perform login to the sportsbook.
        Returns True if login successful, False otherwise.
        """
        if not self.driver:
            self.initialize()
        
        try:
            logger.info(f"[{self.bookmaker}] Attempting login...")
            jittered_delay(self.config.min_delay_seconds, self.config.max_delay_seconds)
            
            success = self._perform_login()
            
            if success:
                self.session_status.logged_in = True
                self.session_status.session_valid = True
                self.session_status.last_login_at = datetime.utcnow()
                self.session_status.login_failures = 0
                self.session_status.error = None
                logger.info(f"[{self.bookmaker}] Login successful")
            else:
                self.session_status.login_failures += 1
                self.session_status.error = "Login failed"
                logger.warning(f"[{self.bookmaker}] Login failed")
            
            return success
            
        except Exception as e:
            self.session_status.login_failures += 1
            self.session_status.error = str(e)
            logger.error(f"[{self.bookmaker}] Login error: {e}")
            return False
    
    def scrape(self) -> ScrapeResult:
        """
        Scrape current odds from the sportsbook.
        Returns a ScrapeResult with the collected odds.
        """
        start_time = time.time()
        
        if not self.session_status.session_valid:
            if not self.login():
                return ScrapeResult(
                    bookmaker=self.bookmaker,
                    success=False,
                    error="Not logged in and login failed",
                )
        
        try:
            jittered_delay(self.config.min_delay_seconds, self.config.max_delay_seconds)
            
            odds = self._scrape_odds()
            duration_ms = int((time.time() - start_time) * 1000)
            
            self._scrape_count += 1
            self.session_status.last_activity_at = datetime.utcnow()
            
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=True,
                odds=odds,
                duration_ms=duration_ms,
                page_url=self.driver.current_url if self.driver else None,
            )
            
        except Exception as e:
            self._error_count += 1
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[{self.bookmaker}] Scrape error: {e}")
            
            # Check if session expired
            if self._is_session_expired():
                self.session_status.session_valid = False
            
            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )
    
    @abstractmethod
    def _perform_login(self) -> bool:
        """
        Implement the actual login flow for the sportsbook.
        Must be overridden by subclasses.
        """
        pass
    
    @abstractmethod
    def _scrape_odds(self) -> List[MarketOdds]:
        """
        Implement the actual odds scraping for the sportsbook.
        Must be overridden by subclasses.
        """
        pass
    
    def _is_session_expired(self) -> bool:
        """Check if the current session has expired. Override if needed."""
        return False

