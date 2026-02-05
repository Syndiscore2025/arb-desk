"""
Generic Playwright adapter with CSS/XPath selector-based configuration.

This adapter can be configured via extra_config to work with any sportsbook
without writing custom code. Supports:
- Configurable selectors for login and odds extraction
- TOTP, SMS, and Email 2FA
- Human-like behavior simulation
- Ban detection and recovery
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import List, Optional

import httpx
import pyotp

from shared.schemas import MarketOdds, TwoFactorConfig
from .playwright_adapter import PlaywrightFeedAdapter
from ..stealth_playwright import async_jittered_delay

logger = logging.getLogger(__name__)


class PlaywrightGenericAdapter(PlaywrightFeedAdapter):
    """
    Generic Playwright adapter configured via selectors.
    
    Required extra_config fields:
    - username_selector: CSS selector for username field
    - password_selector: CSS selector for password field
    - submit_selector: CSS selector for submit button
    - login_success_selector: CSS selector that appears after successful login
    - event_container_selector: CSS selector for event containers
    - selection_selector: CSS selector for selections within events
    - odds_selector: CSS selector for odds values
    
    Optional extra_config fields:
    - totp_selector: CSS selector for TOTP input (if 2FA enabled)
    - totp_submit_selector: CSS selector for TOTP submit button
    - event_id_selector: CSS selector for event ID
    - sport_selector: CSS selector for sport name
    - market_selector: CSS selector for market type
    - selection_name_selector: CSS selector for selection name
    """
    
    async def _perform_login(self) -> bool:
        """Perform login with human-like behavior."""
        try:
            config = self.config.extra_config
            
            # Navigate to login page
            logger.info(f"[{self.bookmaker}] Navigating to login page...")
            await self.browser.page.goto(self.config.login_url, wait_until="networkidle")
            
            # Random scroll to simulate reading
            await self.browser.human_scroll(300)
            await async_jittered_delay(1, 3)
            
            # Type username with human-like behavior
            logger.info(f"[{self.bookmaker}] Entering username...")
            await self.browser.human_type(
                config["username_selector"],
                self.credentials.username,
                delay_range=(80, 200)
            )
            
            await async_jittered_delay(0.5, 1.5)
            
            # Type password
            logger.info(f"[{self.bookmaker}] Entering password...")
            await self.browser.human_type(
                config["password_selector"],
                self.credentials.password,
                delay_range=(80, 200)
            )
            
            await async_jittered_delay(1, 2)
            
            # Click submit with mouse movement
            submit_element = await self.browser.page.wait_for_selector(config["submit_selector"])
            box = await submit_element.bounding_box()
            if box:
                await self.browser.human_mouse_move(
                    int(box["x"] + box["width"] / 2),
                    int(box["y"] + box["height"] / 2)
                )
            
            await submit_element.click()
            logger.info(f"[{self.bookmaker}] Submitted login form")
            
            # Wait for navigation
            await async_jittered_delay(2, 4)
            
            # Handle 2FA if configured
            if await self._handle_2fa():
                logger.info(f"[{self.bookmaker}] 2FA completed")
            
            # Check for login success
            try:
                await self.browser.page.wait_for_selector(
                    config["login_success_selector"],
                    timeout=10000
                )
                logger.info(f"[{self.bookmaker}] Login success indicator found")
                return True
            except Exception:
                logger.error(f"[{self.bookmaker}] Login success indicator not found")
                return False
                
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Login failed: {e}")
            return False
    
    async def _handle_2fa(self) -> bool:
        """Handle 2FA if configured."""
        config = self.config.extra_config
        
        # Check if TOTP selector exists on page
        if "totp_selector" not in config:
            return True  # No 2FA configured
        
        try:
            # Wait for TOTP input to appear
            totp_input = await self.browser.page.wait_for_selector(
                config["totp_selector"],
                timeout=5000
            )
        except Exception:
            # TOTP input didn't appear, assume no 2FA needed
            return True
        
        logger.info(f"[{self.bookmaker}] 2FA required")
        
        # Get 2FA code based on method
        two_factor = self.credentials.two_factor
        if two_factor:
            if two_factor.method == "totp":
                code = self._generate_totp_code(two_factor.totp_secret)
            elif two_factor.method == "sms":
                code = await self._poll_sms_code(two_factor)
            elif two_factor.method == "email":
                code = await self._poll_email_code(two_factor)
            else:
                logger.error(f"[{self.bookmaker}] Unknown 2FA method: {two_factor.method}")
                return False
        elif self.credentials.totp_secret:
            # Backward compatibility
            code = self._generate_totp_code(self.credentials.totp_secret)
        else:
            logger.error(f"[{self.bookmaker}] 2FA required but no credentials configured")
            return False
        
        if not code:
            logger.error(f"[{self.bookmaker}] Failed to get 2FA code")
            return False
        
        # Enter 2FA code
        logger.info(f"[{self.bookmaker}] Entering 2FA code...")
        await self.browser.human_type(config["totp_selector"], code, delay_range=(100, 250))
        
        await async_jittered_delay(0.5, 1.5)
        
        # Submit 2FA
        if "totp_submit_selector" in config:
            submit_element = await self.browser.page.wait_for_selector(config["totp_submit_selector"])
            await submit_element.click()
        
        await async_jittered_delay(2, 4)

        return True

    def _generate_totp_code(self, secret: str) -> Optional[str]:
        """Generate TOTP code from secret."""
        try:
            totp = pyotp.TOTP(secret)
            return totp.now()
        except Exception as e:
            logger.error(f"[{self.bookmaker}] TOTP generation failed: {e}")
            return None

    async def _poll_sms_code(self, two_factor) -> Optional[str]:
        """Poll SMS API for 2FA code."""
        return await self._poll_2fa_api(two_factor)

    async def _poll_email_code(self, two_factor) -> Optional[str]:
        """Poll Email API for 2FA code."""
        return await self._poll_2fa_api(two_factor)

    async def _poll_2fa_api(self, two_factor) -> Optional[str]:
        """Poll API endpoint for 2FA code."""
        import httpx

        headers = {"Authorization": f"Bearer {two_factor.api_key}"}
        if two_factor.api_headers:
            headers.update(two_factor.api_headers)

        timeout = two_factor.poll_timeout_seconds
        interval = two_factor.poll_interval_seconds
        elapsed = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while elapsed < timeout:
                try:
                    response = await client.get(two_factor.api_url, headers=headers)
                    if response.status_code == 200:
                        content_type = response.headers.get("content-type", "")
                        if "application/json" in content_type:
                            data = response.json()
                        else:
                            data = response.text

                        code = self._extract_code(data, two_factor.code_regex)
                        if code:
                            logger.info(f"[{self.bookmaker}] Got 2FA code from API")
                            return code
                except Exception as e:
                    logger.warning(f"[{self.bookmaker}] 2FA API poll error: {e}")

                await async_jittered_delay(interval, interval + 1)
                elapsed += interval

        logger.error(f"[{self.bookmaker}] 2FA code polling timed out")
        return None

    def _extract_code(self, data, code_regex: Optional[str]) -> Optional[str]:
        """Extract 2FA code from API response."""
        if isinstance(data, dict):
            for key in ["code", "otp", "verification_code", "2fa_code", "token"]:
                if key in data:
                    return str(data[key])

        text = str(data)
        if code_regex:
            match = re.search(code_regex, text)
            if match:
                return match.group(1) if match.groups() else match.group(0)

        match = re.search(r"\b(\d{6})\b", text)
        if match:
            return match.group(1)

        return None

    async def _scrape_odds(self) -> List[MarketOdds]:
        """Scrape odds from configured pages."""
        config = self.config.extra_config
        all_odds = []

        for url in self.config.odds_urls:
            logger.info(f"[{self.bookmaker}] Scraping odds from {url}")

            await self.browser.page.goto(url, wait_until="networkidle")
            await async_jittered_delay(2, 4)

            # Scroll to load dynamic content
            await self.browser.human_scroll(500)
            await async_jittered_delay(1, 2)

            odds = await self._extract_odds_from_page(config)
            all_odds.extend(odds)

            logger.info(f"[{self.bookmaker}] Extracted {len(odds)} odds from {url}")

        return all_odds

    async def _extract_odds_from_page(self, config: dict) -> List[MarketOdds]:
        """Extract odds using configured selectors."""
        odds_list = []

        try:
            events = await self.browser.page.query_selector_all(
                config["event_container_selector"]
            )

            for event in events:
                try:
                    # Get event ID
                    event_id_attr = config.get("event_id_attr", "data-event-id")
                    event_id = await event.get_attribute(event_id_attr)
                    if not event_id:
                        event_id = f"event_{hash(await event.inner_text())}"

                    # Get sport
                    sport = "unknown"
                    if "sport_selector" in config:
                        sport_el = await event.query_selector(config["sport_selector"])
                        if sport_el:
                            sport = (await sport_el.inner_text()).strip().lower()

                    # Get market type
                    market = "match_winner"
                    if "market_selector" in config:
                        market_el = await event.query_selector(config["market_selector"])
                        if market_el:
                            market = (await market_el.inner_text()).strip().lower()

                    # Find selections
                    selections = await event.query_selector_all(config["selection_selector"])

                    for selection in selections:
                        try:
                            name_attr = config.get("selection_name_attr")
                            if name_attr:
                                selection_name = await selection.get_attribute(name_attr)
                            else:
                                selection_name = (await selection.inner_text()).strip()

                            odds_el = await selection.query_selector(config["odds_selector"])
                            if not odds_el:
                                odds_el = selection

                            odds_text = (await odds_el.inner_text()).strip()
                            odds_decimal = self._parse_odds(odds_text)

                            if odds_decimal and odds_decimal > 1.0:
                                odds_list.append(MarketOdds(
                                    event_id=event_id,
                                    sport=sport,
                                    market=market,
                                    selection=selection_name,
                                    odds_decimal=odds_decimal,
                                    bookmaker=self.bookmaker,
                                ))
                        except Exception as e:
                            logger.debug(f"[{self.bookmaker}] Error parsing selection: {e}")

                except Exception as e:
                    logger.debug(f"[{self.bookmaker}] Error parsing event: {e}")

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error extracting odds: {e}")

        return odds_list

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds text to decimal format."""
        try:
            cleaned = re.sub(r"[^\d.+\-/]", "", odds_text)

            if not cleaned:
                return None

            # Decimal format (2.50)
            if re.match(r"^\d+\.?\d*$", cleaned):
                return float(cleaned)

            # American format (+150, -110)
            if cleaned.startswith("+"):
                return 1 + (float(cleaned[1:]) / 100)
            elif cleaned.startswith("-"):
                return 1 + (100 / float(cleaned[1:]))

            # Fractional format (5/2)
            if "/" in cleaned:
                num, denom = cleaned.split("/")
                return 1 + (float(num) / float(denom))

            return float(cleaned)

        except Exception:
            return None
