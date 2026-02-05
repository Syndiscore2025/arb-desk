"""
Odds Boost / Promo Detector

Detects and tracks odds boosts and promotions:
- Daily boosts (enhanced odds on specific markets)
- Profit boosts (% increase on winnings)
- Risk-free bets
- Deposit matches

Boosts typically have 5-20%+ edge when hedged on another book.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


# Common boost indicators in page elements
BOOST_INDICATORS = [
    "boost", "boosted", "enhanced", "profit boost", "odds boost",
    "super boost", "flash boost", "mega boost", "lightning boost",
    "special", "promo", "promotion", "featured", "exclusive",
]


class BoostDetector:
    """
    Detects odds boosts and promotional odds.

    Boosts have 5-20%+ edge when hedged properly.
    """

    def __init__(self, bookmaker: str, config: Dict[str, Any]):
        self.bookmaker = bookmaker
        self.config = config
        self.selectors = {
            "boost_container": config.get("boost_container_selector", ".boost, .promo"),
            "boost_badge": config.get("boost_badge_selector", ".boost-badge, .enhanced"),
            "original_odds": config.get("original_odds_selector", ".original-odds, .was-price"),
            "boosted_odds": config.get("boosted_odds_selector", ".boosted-odds, .now-price"),
            "event_name": config.get("boost_event_selector", ".event-name"),
            "selection_name": config.get("boost_selection_selector", ".selection-name"),
            "expires": config.get("boost_expires_selector", ".expires, .countdown"),
        }

    async def detect_boosts(self, page, sport: str = "all") -> List[MarketOdds]:
        """
        Detect all boosted odds on the current page.
        """
        odds_list: List[MarketOdds] = []

        try:
            # First try dedicated boost containers
            containers = await page.query_selector_all(self.selectors["boost_container"])
            logger.info(f"[{self.bookmaker}] Found {len(containers)} boost containers")

            for container in containers:
                try:
                    boost_odds = await self._parse_boost_container(container, sport)
                    if boost_odds:
                        odds_list.append(boost_odds)
                except Exception as e:
                    logger.warning(f"[{self.bookmaker}] Failed to parse boost: {e}")

            # Also scan for boost badges on regular odds
            if not containers:
                badge_odds = await self._scan_for_boost_badges(page, sport)
                odds_list.extend(badge_odds)

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Boost detection failed: {e}")

        logger.info(f"[{self.bookmaker}] Detected {len(odds_list)} boosted odds")
        return odds_list

    async def _parse_boost_container(self, container, sport: str) -> Optional[MarketOdds]:
        """Parse a boost container into MarketOdds."""

        # Get event name
        event_elem = await container.query_selector(self.selectors["event_name"])
        event_name = await event_elem.inner_text() if event_elem else "Boost"
        event_id = self._generate_event_id(event_name)

        # Get selection name
        sel_elem = await container.query_selector(self.selectors["selection_name"])
        selection = await sel_elem.inner_text() if sel_elem else "Boosted Selection"

        # Get original (pre-boost) odds
        orig_elem = await container.query_selector(self.selectors["original_odds"])
        original_odds = None
        if orig_elem:
            orig_text = await orig_elem.inner_text()
            original_odds = self._parse_odds(orig_text)

        # Get boosted odds
        boost_elem = await container.query_selector(self.selectors["boosted_odds"])
        if not boost_elem:
            # Try generic odds selector
            boost_elem = await container.query_selector(".odds, .price")

        if not boost_elem:
            return None

        boost_text = await boost_elem.inner_text()
        boosted_odds = self._parse_odds(boost_text)

        if not boosted_odds or boosted_odds <= 1.0:
            return None

        # Get expiration if available
        expires_at = None
        exp_elem = await container.query_selector(self.selectors["expires"])
        if exp_elem:
            exp_text = await exp_elem.inner_text()
            expires_at = self._parse_expiration(exp_text)

        # Calculate boost value (edge)
        boost_value = None
        if original_odds and original_odds > 1.0:
            boost_value = ((boosted_odds - 1) / (original_odds - 1) - 1) * 100

        logger.info(f"[{self.bookmaker}] Found boost: {selection} @ {boosted_odds} "
                   f"(was {original_odds}, +{boost_value:.1f}% boost)")

        return MarketOdds(
            event_id=event_id,
            sport=sport,
            market="boost",
            bookmaker=self.bookmaker,
            selection=selection.strip(),
            odds_decimal=boosted_odds,
            market_type="boost",
            is_boosted=True,
            original_odds=original_odds,
            expires_at=expires_at,
        )

    async def _scan_for_boost_badges(self, page, sport: str) -> List[MarketOdds]:
        """Scan page for odds with boost badges/indicators."""
        odds_list = []

        # Look for elements with boost-related classes or text
        badges = await page.query_selector_all(self.selectors["boost_badge"])

        for badge in badges:
            try:
                # Get parent container
                parent = await badge.evaluate_handle("el => el.closest('.outcome, .selection, .event-card')")
                if not parent:
                    continue

                    odds_list.append(MarketOdds(
                        event_id=self._generate_event_id(selection),
                        sport=sport,
                        market="boost",
                        bookmaker=self.bookmaker,
                        selection=selection.strip(),
                        odds_decimal=odds,
                        market_type="boost",
                        is_boosted=True,
                    ))
            except Exception as e:
                logger.warning(f"Badge scan error: {e}")

        return odds_list

    def _generate_event_id(self, name: str) -> str:
        """Generate event ID from name."""
        clean = re.sub(r"[^a-zA-Z0-9]", "-", name.lower())[:50]
        return f"boost-{self.bookmaker}-{clean}"

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds from text (American or Decimal)."""
        odds_text = odds_text.strip()

        if odds_text.startswith("+"):
            try:
                american = int(odds_text[1:])
                return round(1 + (american / 100), 4)
            except ValueError:
                pass
        elif odds_text.startswith("-"):
            try:
                american = int(odds_text[1:])
                return round(1 + (100 / american), 4)
            except ValueError:
                pass

        try:
            decimal = float(re.sub(r"[^\d.]", "", odds_text))
            if decimal > 1.0:
                return round(decimal, 4)
        except ValueError:
            pass

        return None

    def _parse_expiration(self, exp_text: str) -> Optional[datetime]:
        """Parse expiration from countdown or time text."""
        exp_lower = exp_text.lower()
        now = datetime.utcnow()

        # Match patterns like "2h 30m", "1d 5h", "30 mins"
        hours = 0
        minutes = 0

        hour_match = re.search(r"(\d+)\s*h", exp_lower)
        if hour_match:
            hours = int(hour_match.group(1))

        min_match = re.search(r"(\d+)\s*m", exp_lower)
        if min_match:
            minutes = int(min_match.group(1))

        day_match = re.search(r"(\d+)\s*d", exp_lower)
        if day_match:
            hours += int(day_match.group(1)) * 24

        if hours or minutes:
            return now + timedelta(hours=hours, minutes=minutes)

        # Try "today", "tomorrow"
        if "today" in exp_lower:
            return now.replace(hour=23, minute=59, second=59)
        if "tomorrow" in exp_lower:
            return (now + timedelta(days=1)).replace(hour=23, minute=59, second=59)

        return None

    def get_boost_urls(self) -> List[str]:
        """Get boost/promo page URLs."""
        urls = {
            "fanduel": [
                "https://sportsbook.fanduel.com/promos",
                "https://sportsbook.fanduel.com/navigation/boosts",
            ],
            "draftkings": [
                "https://sportsbook.draftkings.com/promos",
                "https://sportsbook.draftkings.com/odds-boosts",
            ],
            "fanatics": [
                "https://sportsbook.fanatics.com/promos",
                "https://sportsbook.fanatics.com/boosts",
            ],
        }
        return urls.get(self.bookmaker.lower(), [])

    def calculate_hedge_profit(
        self,
        boosted_odds: float,
        hedge_odds: float,
        stake: float = 100
    ) -> Tuple[float, float, float]:
        """
        Calculate profit from hedging a boosted bet.

        Returns: (hedge_stake, guaranteed_profit, profit_percentage)
        """
        # Stake on boosted side: stake
        # Stake on hedge: stake * boosted_odds / hedge_odds
        hedge_stake = stake * (boosted_odds - 1) / (hedge_odds - 1)
        total_stake = stake + hedge_stake

        # If boost wins: stake * boosted_odds - hedge_stake
        # If hedge wins: hedge_stake * hedge_odds - stake
        boost_profit = stake * boosted_odds - total_stake
        hedge_profit = hedge_stake * hedge_odds - total_stake

        # Guaranteed profit is the minimum
        guaranteed_profit = min(boost_profit, hedge_profit)
        profit_pct = (guaranteed_profit / total_stake) * 100

        return hedge_stake, guaranteed_profit, profit_pct
