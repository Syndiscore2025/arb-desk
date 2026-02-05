"""
Alternate Lines Scraper

Scrapes alternate spreads and totals:
- Alt spreads: -1.5, -2.5, -3.5, ... -10.5 (with varying odds)
- Alt totals: O/U 42.5, 43.5, 44.5, ... (with varying odds)

Alt lines typically have 2-6% edge due to inefficient pricing.
Books often misprice alt lines vs main lines.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


class AltLinesScraper:
    """
    Scrapes alternate spread and total lines.

    Alt lines are less efficient with 2-6% edge potential.
    """

    def __init__(self, bookmaker: str, config: Dict[str, Any]):
        self.bookmaker = bookmaker
        self.config = config
        self.selectors = {
            "alt_container": config.get("alt_container_selector", ".alt-lines"),
            "spread_tab": config.get("spread_tab_selector", "[data-tab='spread']"),
            "total_tab": config.get("total_tab_selector", "[data-tab='total']"),
            "line_row": config.get("line_row_selector", ".line-row"),
            "line_value": config.get("line_value_selector", ".line-value"),
            "selection_home": config.get("alt_home_selector", ".home .odds"),
            "selection_away": config.get("alt_away_selector", ".away .odds"),
            "over_odds": config.get("alt_over_selector", ".over .odds"),
            "under_odds": config.get("alt_under_selector", ".under .odds"),
            "event_name": config.get("event_name_selector", ".event-name"),
        }

    async def scrape_alt_lines(self, page, sport: str) -> List[MarketOdds]:
        """
        Scrape all alternate lines from the current page.

        Returns:
            List of MarketOdds for each alt line selection
        """
        odds_list: List[MarketOdds] = []

        try:
            # Try to find alt lines container
            containers = await page.query_selector_all(self.selectors["alt_container"])

            if not containers:
                # Try clicking on alt lines tab if exists
                alt_tab = await page.query_selector("[data-tab='alt-lines'], .alt-tab")
                if alt_tab:
                    await alt_tab.click()
                    await page.wait_for_timeout(1500)
                    containers = await page.query_selector_all(self.selectors["alt_container"])

            logger.info(f"[{self.bookmaker}] Found {len(containers)} alt line containers")

            for container in containers:
                try:
                    # Scrape alt spreads
                    spread_odds = await self._scrape_alt_spreads(container, sport)
                    odds_list.extend(spread_odds)

                    # Scrape alt totals
                    total_odds = await self._scrape_alt_totals(container, sport)
                    odds_list.extend(total_odds)
                except Exception as e:
                    logger.warning(f"[{self.bookmaker}] Failed to parse alt lines: {e}")

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Alt lines scrape failed: {e}")

        logger.info(f"[{self.bookmaker}] Scraped {len(odds_list)} alt line odds")
        return odds_list

    async def _scrape_alt_spreads(self, container, sport: str) -> List[MarketOdds]:
        """Scrape alternate spreads from container."""
        odds_list = []

        # Get event info
        event_elem = await container.query_selector(self.selectors["event_name"])
        event_name = await event_elem.inner_text() if event_elem else "Unknown"
        event_id = self._generate_event_id(event_name, "spread")

        # Click spreads tab if needed
        spread_tab = await container.query_selector(self.selectors["spread_tab"])
        if spread_tab:
            await spread_tab.click()
            await container.page().wait_for_timeout(500)

        # Get all line rows
        rows = await container.query_selector_all(self.selectors["line_row"])

        for row in rows:
            try:
                # Get line value (e.g., -3.5)
                line_elem = await row.query_selector(self.selectors["line_value"])
                if not line_elem:
                    continue
                line_text = await line_elem.inner_text()
                line = self._parse_line(line_text)

                # Get home spread odds
                home_elem = await row.query_selector(self.selectors["selection_home"])
                if home_elem:
                    odds_text = await home_elem.inner_text()
                    odds = self._parse_odds(odds_text)
                    if odds and odds > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market="alt_spread",
                            bookmaker=self.bookmaker,
                            selection=f"Home {line:+.1f}",
                            odds_decimal=odds,
                            market_type="spread",
                            line=line,
                        ))

                # Get away spread odds (opposite line)
                away_elem = await row.query_selector(self.selectors["selection_away"])
                if away_elem:
                    odds_text = await away_elem.inner_text()
                    odds = self._parse_odds(odds_text)
                    if odds and odds > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market="alt_spread",
                            bookmaker=self.bookmaker,
                            selection=f"Away {-line:+.1f}",
                            odds_decimal=odds,
                            market_type="spread",
                            line=-line,
                        ))
            except Exception as e:
                logger.warning(f"Failed to parse spread row: {e}")


    async def _scrape_alt_totals(self, container, sport: str) -> List[MarketOdds]:
        """Scrape alternate totals from container."""
        odds_list = []

        # Get event info
        event_elem = await container.query_selector(self.selectors["event_name"])
        event_name = await event_elem.inner_text() if event_elem else "Unknown"
        event_id = self._generate_event_id(event_name, "total")

        # Click totals tab if needed
        total_tab = await container.query_selector(self.selectors["total_tab"])
        if total_tab:
            await total_tab.click()
            await container.page().wait_for_timeout(500)

        # Get all line rows
        rows = await container.query_selector_all(self.selectors["line_row"])

        for row in rows:
            try:
                # Get line value (e.g., 45.5)
                line_elem = await row.query_selector(self.selectors["line_value"])
                if not line_elem:
                    continue
                line_text = await line_elem.inner_text()
                line = abs(self._parse_line(line_text))

                # Get over odds
                over_elem = await row.query_selector(self.selectors["over_odds"])
                if over_elem:
                    odds_text = await over_elem.inner_text()
                    odds = self._parse_odds(odds_text)
                    if odds and odds > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market="alt_total",
                            bookmaker=self.bookmaker,
                            selection=f"Over {line}",
                            odds_decimal=odds,
                            market_type="total",
                            line=line,
                        ))

                # Get under odds
                under_elem = await row.query_selector(self.selectors["under_odds"])
                if under_elem:
                    odds_text = await under_elem.inner_text()
                    odds = self._parse_odds(odds_text)
                    if odds and odds > 1.0:
                        odds_list.append(MarketOdds(
                            event_id=event_id,
                            sport=sport,
                            market="alt_total",
                            bookmaker=self.bookmaker,
                            selection=f"Under {line}",
                            odds_decimal=odds,
                            market_type="total",
                            line=line,
                        ))
            except Exception as e:
                logger.warning(f"Failed to parse total row: {e}")

        return odds_list

    def _generate_event_id(self, event_name: str, market_type: str) -> str:
        """Generate unique event ID."""
        clean = re.sub(r"[^a-zA-Z0-9]", "-", event_name.lower())
        return f"alt-{market_type}-{clean}"

    def _parse_line(self, line_text: str) -> float:
        """Parse line value from text."""
        match = re.search(r"([+-]?[\d.]+)", line_text)
        if match:
            return float(match.group(1))
        return 0.0

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

    def get_alt_line_urls(self, sport: str) -> List[str]:
        """Get alt line URLs for a sport."""
        base_urls = {
            "fanduel": {
                "nba": "https://sportsbook.fanduel.com/navigation/nba?tab=alternate-lines",
                "nfl": "https://sportsbook.fanduel.com/navigation/nfl?tab=alternate-lines",
            },
            "draftkings": {
                "nba": "https://sportsbook.draftkings.com/leagues/basketball/nba?subcategory=alternate",
                "nfl": "https://sportsbook.draftkings.com/leagues/football/nfl?subcategory=alternate",
            },
            "fanatics": {
                "nba": "https://sportsbook.fanatics.com/sports/basketball/nba/alternate-lines",
                "nfl": "https://sportsbook.fanatics.com/sports/football/nfl/alternate-lines",
            },
        }

        book_urls = base_urls.get(self.bookmaker.lower(), {})
        url = book_urls.get(sport.lower())
        return [url] if url else []

