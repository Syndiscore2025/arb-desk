"""
Bet Executor - Place bets on sportsbooks via browser automation.

This module handles bet placement when a user responds to an arb alert
with a stake amount. It uses the existing Playwright adapter for stealth.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.playwright_adapter import PlaywrightFeedAdapter

from shared.schemas import BetRequest, BetResponse, FeedConfig

logger = logging.getLogger(__name__)


class BetExecutor:
    """
    Execute bets on sportsbooks via browser automation.
    
    Uses the Playwright adapter's stealth browser to:
    1. Navigate to the bet slip
    2. Enter stake amount
    3. Verify odds haven't changed significantly
    4. Place the bet
    5. Capture confirmation
    """
    
    # Tolerance for odds change (0.05 = 5% variance allowed)
    ODDS_TOLERANCE = 0.05
    
    def __init__(
        self,
        adapter: "PlaywrightFeedAdapter",
        config: FeedConfig,
    ):
        self.adapter = adapter
        self.config = config
        self._bets_placed = 0
        self._bets_failed = 0
    
    @property
    def bookmaker(self) -> str:
        return self.config.bookmaker
    
    async def place_bet(self, request: BetRequest) -> BetResponse:
        """
        Place a bet on the sportsbook.
        
        Args:
            request: BetRequest with event, selection, odds, stake details
            
        Returns:
            BetResponse with success/failure and confirmation details
        """
        logger.info(f"[{self.bookmaker}] Placing bet: {request.selection} @ "
                   f"{request.odds_decimal:.2f} for ${request.stake_amount:.2f}")
        
        try:
            # Ensure browser is initialized and logged in
            if not self.adapter.browser or not self.adapter.browser.page:
                await self.adapter.initialize()
                if not await self.adapter.login():
                    return self._error_response(request, "Failed to login")
            
            # Get selectors from config
            selectors = self.config.extra_config
            
            # Navigate to selection (deep link or search)
            deep_link = selectors.get("bet_deep_link_template")
            if deep_link:
                url = deep_link.format(
                    event_id=request.event_id,
                    selection=request.selection,
                )
                await self.adapter.browser.page.goto(url)
                await asyncio.sleep(2)
            else:
                # Navigate to event page and click selection
                event_url = selectors.get("event_url_template", "").format(
                    event_id=request.event_id
                )
                if event_url:
                    await self.adapter.browser.page.goto(event_url)
                    await asyncio.sleep(2)
                    
                    # Click on the selection to add to bet slip
                    selection_selector = selectors.get("selection_click_selector", "")
                    if selection_selector:
                        # Find by selection name
                        elements = await self.adapter.browser.page.query_selector_all(
                            selection_selector
                        )
                        for el in elements:
                            text = await el.inner_text()
                            if request.selection.lower() in text.lower():
                                await el.click()
                                break
            
            await asyncio.sleep(1)
            
            # Verify current odds
            odds_selector = selectors.get("betslip_odds_selector", ".bet-odds")
            displayed_odds = await self._get_displayed_odds(odds_selector)
            
            if displayed_odds:
                odds_diff = abs(displayed_odds - request.odds_decimal) / request.odds_decimal
                if odds_diff > self.ODDS_TOLERANCE:
                    self._bets_failed += 1
                    return BetResponse(
                        bet_id=request.bet_id,
                        bookmaker=request.bookmaker,
                        success=False,
                        actual_odds=displayed_odds,
                        error=f"Odds changed: {request.odds_decimal:.2f} → {displayed_odds:.2f} ({odds_diff*100:.1f}%)"
                    )
            
            # Enter stake amount
            stake_selector = selectors.get("stake_input_selector", "input.stake")
            await self.adapter.browser.human_type(
                stake_selector, 
                str(request.stake_amount),
                delay_range=(80, 150)
            )
            await asyncio.sleep(0.5)
            
            # Click place bet button
            place_btn = selectors.get("place_bet_button_selector", "button.place-bet")
            await self.adapter.browser.page.click(place_btn)
            
            # Wait for confirmation
            confirm_selector = selectors.get("confirmation_selector", ".bet-confirmed")
            try:
                await self.adapter.browser.page.wait_for_selector(
                    confirm_selector, 
                    timeout=15000
                )
            except Exception:
                self._bets_failed += 1
                return BetResponse(
                    bet_id=request.bet_id,
                    bookmaker=request.bookmaker,
                    success=False,
                    error="Bet confirmation not received within 15s"
                )
            
            # Extract confirmation number
            confirm_el = await self.adapter.browser.page.query_selector(confirm_selector)
            confirmation_text = await confirm_el.inner_text() if confirm_el else None
            
            self._bets_placed += 1
            
            return BetResponse(
                bet_id=request.bet_id,
                bookmaker=request.bookmaker,
                success=True,
                confirmation_number=confirmation_text,
                actual_odds=displayed_odds or request.odds_decimal,
                actual_stake=request.stake_amount,
                potential_payout=request.stake_amount * (displayed_odds or request.odds_decimal),
            )
            
        except Exception as e:
            self._bets_failed += 1
            logger.error(f"[{self.bookmaker}] Bet placement error: {e}")
            return self._error_response(request, str(e))

    async def _get_displayed_odds(self, selector: str) -> Optional[float]:
        """Extract and parse the currently displayed odds."""
        try:
            el = await self.adapter.browser.page.query_selector(selector)
            if not el:
                return None

            text = await el.inner_text()
            return self._parse_odds(text.strip())
        except Exception as e:
            logger.warning(f"[{self.bookmaker}] Could not get displayed odds: {e}")
            return None

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds text to decimal format."""
        try:
            text = odds_text.strip()

            # Decimal odds (e.g., "2.50")
            if "." in text and not text.startswith(("+", "-")):
                return float(text)

            # American odds (e.g., "+150", "-110")
            if text.startswith("+"):
                american = int(text[1:])
                return 1 + (american / 100)
            elif text.startswith("-"):
                american = int(text[1:])
                return 1 + (100 / american)

            # Fractional odds (e.g., "5/2")
            if "/" in text:
                num, den = text.split("/")
                return 1 + (float(num) / float(den))

            # Try direct float conversion
            return float(text)

        except Exception:
            return None

    def _error_response(self, request: BetRequest, error: str) -> BetResponse:
        """Create an error response."""
        return BetResponse(
            bet_id=request.bet_id,
            bookmaker=request.bookmaker,
            success=False,
            error=error,
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get bet executor statistics."""
        return {
            "bookmaker": self.bookmaker,
            "bets_placed": self._bets_placed,
            "bets_failed": self._bets_failed,
            "success_rate": (
                self._bets_placed / (self._bets_placed + self._bets_failed) * 100
                if (self._bets_placed + self._bets_failed) > 0
                else 0.0
            ),
        }

