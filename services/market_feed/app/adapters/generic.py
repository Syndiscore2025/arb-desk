"""
Generic sportsbook adapter that can be configured via selectors.
Allows scraping any sportsbook by providing CSS/XPath selectors in config.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import pyotp
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, NoSuchElementException

from shared.schemas import BookmakerCredentials, FeedConfig, MarketOdds, TwoFactorConfig
from .base import BaseFeedAdapter
from ..stealth import jittered_delay

logger = logging.getLogger(__name__)


class GenericSportsbookAdapter(BaseFeedAdapter):
    """
    A configurable adapter that works with any sportsbook via CSS/XPath selectors.
    
    Required extra_config keys:
        - login_url: URL of the login page
        - username_selector: CSS selector for username input
        - password_selector: CSS selector for password input
        - submit_selector: CSS selector for login submit button
        - login_success_selector: CSS selector that appears after successful login
        
    Optional extra_config keys:
        - odds_page_url: URL of the main odds page
        - event_container_selector: CSS selector for event containers
        - event_id_selector: CSS selector for event ID within container
        - selection_selector: CSS selector for selections within container
        - odds_selector: CSS selector for odds value within selection
        - sport_selector: CSS selector for sport name
        - market_selector: CSS selector for market name
    """
    
    def __init__(self, config: FeedConfig, credentials: BookmakerCredentials):
        super().__init__(config, credentials)
        self.selectors = config.extra_config
    
    def _get_selector(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a selector from config."""
        return self.selectors.get(key, default)
    
    def _perform_login(self) -> bool:
        """Perform login using configured selectors."""
        login_url = self._get_selector("login_url") or self.config.login_url
        if not login_url:
            logger.error(f"[{self.bookmaker}] No login URL configured")
            return False
        
        try:
            self.driver.get(login_url)
            jittered_delay(2, 4)
            
            # Find and fill username
            username_sel = self._get_selector("username_selector")
            if username_sel:
                username_input = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, username_sel))
                )
                username_input.clear()
                self._human_type(username_input, self.credentials.username)
                jittered_delay(0.5, 1.5)
            
            # Find and fill password
            password_sel = self._get_selector("password_selector")
            if password_sel:
                password_input = self.driver.find_element(By.CSS_SELECTOR, password_sel)
                password_input.clear()
                self._human_type(password_input, self.credentials.password)
                jittered_delay(0.5, 1.5)
            
            # Click submit
            submit_sel = self._get_selector("submit_selector")
            if submit_sel:
                submit_btn = self.driver.find_element(By.CSS_SELECTOR, submit_sel)
                submit_btn.click()
                jittered_delay(3, 6)

            # Handle 2FA if configured
            twofa_sel = self._get_selector("totp_selector") or self._get_selector("twofa_selector")
            if twofa_sel and self._has_2fa_config():
                if not self._handle_2fa(twofa_sel):
                    return False

            # Verify login success
            success_sel = self._get_selector("login_success_selector")
            if success_sel:
                try:
                    WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, success_sel))
                    )
                    return True
                except TimeoutException:
                    logger.warning(f"[{self.bookmaker}] Login success element not found")
                    return False

            # If no success selector, assume login worked
            return True
            
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Login error: {e}")
            return False
    
    def _human_type(self, element, text: str) -> None:
        """Type text with human-like delays between keystrokes."""
        import random
        for char in text:
            element.send_keys(char)
            delay = random.uniform(0.05, 0.15)
            time.sleep(delay)

    def _has_2fa_config(self) -> bool:
        """Check if 2FA is configured."""
        if self.credentials.totp_secret:
            return True
        if self.credentials.two_factor:
            return True
        return False

    def _handle_2fa(self, twofa_selector: str) -> bool:
        """Handle 2FA code entry. Returns True on success."""
        try:
            # Wait for 2FA input to appear
            twofa_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, twofa_selector))
            )

            # Get the 2FA code
            code = self._get_2fa_code()
            if not code:
                logger.error(f"[{self.bookmaker}] Failed to get 2FA code")
                return False

            logger.info(f"[{self.bookmaker}] Entering 2FA code")
            twofa_input.clear()
            self._human_type(twofa_input, code)
            jittered_delay(0.5, 1.0)

            # Submit the code
            twofa_submit_sel = self._get_selector("totp_submit_selector") or self._get_selector("twofa_submit_selector")
            if twofa_submit_sel:
                twofa_submit = self.driver.find_element(By.CSS_SELECTOR, twofa_submit_sel)
                twofa_submit.click()
            else:
                from selenium.webdriver.common.keys import Keys
                twofa_input.send_keys(Keys.RETURN)

            jittered_delay(3, 5)
            return True

        except TimeoutException:
            logger.debug(f"[{self.bookmaker}] No 2FA prompt detected, continuing")
            return True  # No 2FA required
        except Exception as e:
            logger.error(f"[{self.bookmaker}] 2FA entry failed: {e}")
            return False

    def _get_2fa_code(self) -> Optional[str]:
        """Get 2FA code based on configured method."""
        # Check for simple TOTP secret (backward compatibility)
        if self.credentials.totp_secret and not self.credentials.two_factor:
            totp = pyotp.TOTP(self.credentials.totp_secret)
            return totp.now()

        # Check for advanced 2FA config
        twofa = self.credentials.two_factor
        if not twofa:
            return None

        if twofa.method == "totp":
            if not twofa.totp_secret:
                logger.error(f"[{self.bookmaker}] TOTP method requires totp_secret")
                return None
            totp = pyotp.TOTP(twofa.totp_secret)
            return totp.now()

        elif twofa.method == "sms":
            return self._fetch_code_from_api(twofa, "SMS")

        elif twofa.method == "email":
            return self._fetch_code_from_api(twofa, "email")

        else:
            logger.error(f"[{self.bookmaker}] Unknown 2FA method: {twofa.method}")
            return None

    def _fetch_code_from_api(self, twofa: TwoFactorConfig, method_name: str) -> Optional[str]:
        """Fetch 2FA code from SMS/email API with polling."""
        if not twofa.api_url:
            logger.error(f"[{self.bookmaker}] {method_name} method requires api_url")
            return None

        logger.info(f"[{self.bookmaker}] Waiting for {method_name} code...")

        headers = twofa.api_headers or {}
        if twofa.api_key:
            headers["Authorization"] = f"Bearer {twofa.api_key}"

        # Build request params
        params = {}
        if twofa.phone_number:
            params["phone"] = twofa.phone_number
        if twofa.email_address:
            params["email"] = twofa.email_address

        # Poll for the code
        start_time = time.time()
        timeout = twofa.poll_timeout_seconds
        interval = twofa.poll_interval_seconds

        while (time.time() - start_time) < timeout:
            try:
                with httpx.Client(timeout=10.0) as client:
                    response = client.get(twofa.api_url, headers=headers, params=params)

                    if response.status_code == 200:
                        data = response.text

                        # Extract code using regex if provided
                        if twofa.code_regex:
                            match = re.search(twofa.code_regex, data)
                            if match:
                                code = match.group(1) if match.groups() else match.group(0)
                                logger.info(f"[{self.bookmaker}] Got {method_name} code")
                                return code
                        else:
                            # Try to parse as JSON and look for common fields
                            try:
                                json_data = response.json()
                                code = (
                                    json_data.get("code") or
                                    json_data.get("otp") or
                                    json_data.get("verification_code") or
                                    json_data.get("message", {}).get("code")
                                )
                                if code:
                                    logger.info(f"[{self.bookmaker}] Got {method_name} code")
                                    return str(code)
                            except Exception:
                                pass

                    elif response.status_code == 404:
                        # No message yet, keep polling
                        pass
                    else:
                        logger.warning(f"[{self.bookmaker}] API returned {response.status_code}")

            except Exception as e:
                logger.warning(f"[{self.bookmaker}] API request failed: {e}")

            time.sleep(interval)

        logger.error(f"[{self.bookmaker}] Timeout waiting for {method_name} code")
        return None

    def _scrape_odds(self) -> List[MarketOdds]:
        """Scrape odds using configured selectors."""
        odds_list: List[MarketOdds] = []
        
        # Navigate to odds pages
        odds_urls = self.config.odds_urls or [self._get_selector("odds_page_url")]
        
        for url in odds_urls:
            if not url:
                continue
            
            try:
                self.driver.get(url)
                jittered_delay(2, 4)
                
                page_odds = self._extract_odds_from_page()
                odds_list.extend(page_odds)
                
            except Exception as e:
                logger.error(f"[{self.bookmaker}] Error scraping {url}: {e}")
        
        return odds_list

    def _extract_odds_from_page(self) -> List[MarketOdds]:
        """Extract odds from a single event container."""
        odds_list: List[MarketOdds] = []

        # Get event ID
        event_id_sel = self._get_selector("event_id_selector")
        event_id = self._safe_get_text(event, event_id_sel) or f"event_{id(event)}"

        # Get sport
        sport_sel = self._get_selector("sport_selector")
        sport = self._safe_get_text(event, sport_sel) or "unknown"
        if self.config.sports:
            sport = self.config.sports[0]  # Use configured sport if available

        # Get market
        market_sel = self._get_selector("market_selector")
        market = self._safe_get_text(event, market_sel) or "match_winner"
        if self.config.markets:
            market = self.config.markets[0]

        # Get selections and odds
        selection_sel = self._get_selector("selection_selector")
        odds_sel = self._get_selector("odds_selector")

        if selection_sel:
            selections = event.find_elements(By.CSS_SELECTOR, selection_sel)

            for sel_elem in selections:
                try:
                    selection_name = sel_elem.text.strip() or "unknown"

                    # Get odds value
                    if odds_sel:
                        odds_elem = sel_elem.find_element(By.CSS_SELECTOR, odds_sel)
                        odds_text = odds_elem.text.strip()
                    else:
                        odds_text = sel_elem.text.strip()

                    odds_decimal = self._parse_odds(odds_text)

                    if odds_decimal and odds_decimal > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market=market,
                            bookmaker=self.bookmaker,
                            selection=selection_name,
                            odds_decimal=odds_decimal,
                            captured_at=datetime.utcnow(),
                        ))

                except Exception as e:
                    logger.debug(f"[{self.bookmaker}] Error extracting selection: {e}")

        return odds_list

    def _safe_get_text(self, parent, selector: Optional[str]) -> Optional[str]:
        """Safely get text from an element using a selector."""
        if not selector:
            return None
        try:
            elem = parent.find_element(By.CSS_SELECTOR, selector)
            return elem.text.strip()
        except NoSuchElementException:
            return None

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds text into decimal format."""
        if not odds_text:
            return None

        # Clean the text
        odds_text = odds_text.strip()

        # Try to extract decimal odds directly
        decimal_match = re.search(r'(\d+\.?\d*)', odds_text)
        if decimal_match:
            try:
                value = float(decimal_match.group(1))
                if value > 1.0:
                    return value
            except ValueError:
                pass

        # Try American odds format (+150, -200)
        american_match = re.search(r'([+-])(\d+)', odds_text)
        if american_match:
            sign, num = american_match.groups()
            num = int(num)
            if sign == '+':
                return 1 + (num / 100)
            else:
                return 1 + (100 / num)

        # Try fractional odds (3/1, 1/2)
        frac_match = re.search(r'(\d+)/(\d+)', odds_text)
        if frac_match:
            num, denom = map(int, frac_match.groups())
            if denom > 0:
                return 1 + (num / denom)

        return None

    def _is_session_expired(self) -> bool:
        """Check if session has expired by looking for login elements."""
        login_indicator = self._get_selector("login_required_selector")
        if login_indicator:
            try:
                self.driver.find_element(By.CSS_SELECTOR, login_indicator)
                return True
            except NoSuchElementException:
                return False
        return False
        """Extract odds from the current page."""
        odds_list: List[MarketOdds] = []
        
        event_sel = self._get_selector("event_container_selector")
        if not event_sel:
            logger.warning(f"[{self.bookmaker}] No event container selector configured")
            return odds_list
        
        try:
            events = self.driver.find_elements(By.CSS_SELECTOR, event_sel)
            logger.info(f"[{self.bookmaker}] Found {len(events)} events")
            
            for event in events:
                try:
                    event_odds = self._extract_event_odds(event)
                    odds_list.extend(event_odds)
                except Exception as e:
                    logger.debug(f"[{self.bookmaker}] Error extracting event: {e}")
                    
        except Exception as e:
            logger.error(f"[{self.bookmaker}] Error finding events: {e}")
        
        return odds_list

