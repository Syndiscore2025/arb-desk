"""
Playwright-based adapter with advanced stealth and anti-ban features.

This adapter uses the StealthBrowser class for maximum evasion and account longevity.
Supports:
- Full fingerprint spoofing
- Residential proxy rotation
- Human-like behavior simulation
- CAPTCHA detection and solving
- Ban detection and automatic failover
- Session persistence
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from datetime import datetime
from typing import List, Optional

from shared.schemas import (
    BookmakerCredentials,
    FeedConfig,
    MarketOdds,
    ScrapeResult,
    SessionStatus,
)
from ..stealth_playwright import (
    StealthBrowser,
    ResidentialProxyRotator,
    async_jittered_delay,
)

logger = logging.getLogger(__name__)


class PlaywrightFeedAdapter:
    """
    Advanced Playwright-based feed adapter with comprehensive stealth.
    
    Each sportsbook requires a specific adapter that knows how to:
    1. Log in with credentials (with human-like behavior)
    2. Navigate to odds pages (with realistic delays)
    3. Extract and normalize odds data
    4. Handle session management and auto-relogin
    5. Detect and handle bans/CAPTCHAs
    """
    
    def __init__(
        self,
        config: FeedConfig,
        credentials: BookmakerCredentials,
        proxy_rotator: Optional[ResidentialProxyRotator] = None,
        captcha_api_key: Optional[str] = None,
    ):
        self.config = config
        self.credentials = credentials
        self.proxy_rotator = proxy_rotator
        self.captcha_api_key = captcha_api_key
        
        self.browser: Optional[StealthBrowser] = None
        self.session_status = SessionStatus(
            bookmaker=config.bookmaker,
            logged_in=False,
            session_valid=False,
        )
        
        self._scrape_count = 0
        self._error_count = 0
        self._ban_count = 0
        self._captcha_count = 0
        self._current_proxy = None
    
    @property
    def bookmaker(self) -> str:
        return self.config.bookmaker
    
    async def initialize(self) -> None:
        """Initialize the Playwright browser with stealth settings."""
        if self.browser is not None:
            await self.close()
        
        # Get proxy from rotator if available
        if self.proxy_rotator:
            self._current_proxy = self.proxy_rotator.get_next()
            logger.info(f"[{self.bookmaker}] Using proxy: {self._current_proxy.host if self._current_proxy else 'None'}")
        
        logger.info(f"[{self.bookmaker}] Initializing Playwright stealth browser...")
        self.browser = StealthBrowser(
            bookmaker=self.bookmaker,
            proxy=self._current_proxy or self.config.proxy,
            geo=self.config.extra_config.get("geo", "US"),
            headless=self.config.headless,
            captcha_api_key=self.captcha_api_key,
        )
        
        await self.browser.initialize()
        logger.info(f"[{self.bookmaker}] Playwright browser initialized")
    
    async def close(self) -> None:
        """Close the browser and save session."""
        if self.browser:
            try:
                await self.browser.close()
            except Exception as e:
                logger.warning(f"[{self.bookmaker}] Error closing browser: {e}")
            finally:
                self.browser = None
                self.session_status.logged_in = False
                self.session_status.session_valid = False
    
    async def login(self) -> bool:
        """
        Perform login to the sportsbook with human-like behavior.
        Returns True if login successful, False otherwise.
        """
        if not self.browser:
            await self.initialize()
        
        try:
            logger.info(f"[{self.bookmaker}] Attempting login...")
            await async_jittered_delay(
                self.config.min_delay_seconds,
                self.config.max_delay_seconds
            )
            
            # Check for ban before attempting login
            if await self.browser.detect_ban():
                logger.error(f"[{self.bookmaker}] Ban detected before login")
                self._ban_count += 1
                await self._handle_ban()
                return False
            
            success = await self._perform_login()
            
            if success:
                self.session_status.logged_in = True
                self.session_status.session_valid = True
                self.session_status.last_login_at = datetime.utcnow()
                self.session_status.login_failures = 0
                self.session_status.error = None
                logger.info(f"[{self.bookmaker}] Login successful")
                
                # Mark proxy as successful
                if self.proxy_rotator and self._current_proxy:
                    self.proxy_rotator.mark_success(self._current_proxy)
            else:
                self.session_status.login_failures += 1
                self.session_status.error = "Login failed"
                logger.warning(f"[{self.bookmaker}] Login failed")

            return success

        except Exception as e:
            self.session_status.login_failures += 1
            self.session_status.error = str(e)
            logger.error(f"[{self.bookmaker}] Login error: {e}")

            # Mark proxy as failed
            if self.proxy_rotator and self._current_proxy:
                self.proxy_rotator.mark_failure(self._current_proxy, "login_error")

            return False

    async def scrape(self) -> ScrapeResult:
        """
        Scrape current odds from the sportsbook with ban detection.
        Returns a ScrapeResult with the collected odds.
        """
        start_time = time.time()

        if not self.session_status.session_valid:
            if not await self.login():
                return ScrapeResult(
                    bookmaker=self.bookmaker,
                    success=False,
                    error="Not logged in and login failed",
                )

        try:
            await async_jittered_delay(
                self.config.min_delay_seconds,
                self.config.max_delay_seconds
            )

            # Check for ban before scraping
            if await self.browser.detect_ban():
                logger.error(f"[{self.bookmaker}] Ban detected before scrape")
                self._ban_count += 1
                await self._handle_ban()
                return ScrapeResult(
                    bookmaker=self.bookmaker,
                    success=False,
                    error="Ban detected",
                )

            # Check for CAPTCHA
            if await self.browser._has_captcha():
                logger.warning(f"[{self.bookmaker}] CAPTCHA detected")
                self._captcha_count += 1

                if self.captcha_api_key:
                    if await self.browser.solve_captcha():
                        logger.info(f"[{self.bookmaker}] CAPTCHA solved")
                    else:
                        return ScrapeResult(
                            bookmaker=self.bookmaker,
                            success=False,
                            error="CAPTCHA solving failed",
                        )
                else:
                    return ScrapeResult(
                        bookmaker=self.bookmaker,
                        success=False,
                        error="CAPTCHA detected but no solver configured",
                    )

            odds = await self._scrape_odds()
            duration_ms = int((time.time() - start_time) * 1000)

            self._scrape_count += 1
            self.session_status.last_activity_at = datetime.utcnow()

            # Mark proxy as successful
            if self.proxy_rotator and self._current_proxy:
                self.proxy_rotator.mark_success(self._current_proxy)

            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=True,
                odds=odds,
                duration_ms=duration_ms,
                page_url=self.browser.page.url if self.browser and self.browser.page else None,
            )

        except Exception as e:
            self._error_count += 1
            duration_ms = int((time.time() - start_time) * 1000)
            logger.error(f"[{self.bookmaker}] Scrape error: {e}")

            # Mark proxy as failed
            if self.proxy_rotator and self._current_proxy:
                self.proxy_rotator.mark_failure(self._current_proxy, "scrape_error")

            # Check if session expired
            if await self._is_session_expired():
                self.session_status.session_valid = False

            return ScrapeResult(
                bookmaker=self.bookmaker,
                success=False,
                error=str(e),
                duration_ms=duration_ms,
            )

    async def _handle_ban(self) -> None:
        """
        Handle ban detection by rotating proxy and reinitializing.
        """
        logger.warning(f"[{self.bookmaker}] Handling ban - rotating proxy and reinitializing")

        # Mark current proxy as failed
        if self.proxy_rotator and self._current_proxy:
            self.proxy_rotator.mark_failure(self._current_proxy, "ban")

        # Close current browser
        await self.close()

        # Wait before reinitializing
        await async_jittered_delay(30, 60)

        # Reinitialize with new proxy
        await self.initialize()

    @abstractmethod
    async def _perform_login(self) -> bool:
        """
        Implement the actual login flow for the sportsbook.
        Must be overridden by subclasses.

        Should use:
        - self.browser.human_type() for typing
        - self.browser.human_scroll() for scrolling
        - self.browser.human_mouse_move() for mouse movement
        - async_jittered_delay() for delays
        """
        pass

    @abstractmethod
    async def _scrape_odds(self) -> List[MarketOdds]:
        """
        Implement the actual odds scraping for the sportsbook.
        Must be overridden by subclasses.
        """
        pass

    async def _is_session_expired(self) -> bool:
        """Check if the current session has expired. Override if needed."""
        return False

    def get_stats(self) -> dict:
        """Get adapter statistics."""
        return {
            "bookmaker": self.bookmaker,
            "scrape_count": self._scrape_count,
            "error_count": self._error_count,
            "ban_count": self._ban_count,
            "captcha_count": self._captcha_count,
            "logged_in": self.session_status.logged_in,
            "session_valid": self.session_status.session_valid,
            "login_failures": self.session_status.login_failures,
            "last_activity": self.session_status.last_activity_at.isoformat() if self.session_status.last_activity_at else None,
        }

