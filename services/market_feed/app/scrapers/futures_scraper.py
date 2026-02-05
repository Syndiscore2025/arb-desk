"""
Futures Markets Scraper

Scrapes long-term futures markets:
- Championship winners (Super Bowl, NBA Finals, World Series, Stanley Cup)
- Division winners
- Conference winners
- MVP awards
- Season win totals

Futures typically have 3-10% edge due to varied market maker opinions.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


# Futures market types
FUTURES_MARKETS = {
    "nba": [
        "championship", "conference_winner", "division_winner",
        "mvp", "rookie_of_year", "season_wins",
    ],
    "nfl": [
        "super_bowl", "conference_winner", "division_winner",
        "mvp", "offensive_roy", "defensive_roy", "season_wins",
    ],
    "mlb": [
        "world_series", "league_winner", "division_winner",
        "mvp", "cy_young", "season_wins",
    ],
    "nhl": [
        "stanley_cup", "conference_winner", "division_winner",
        "hart_trophy", "season_wins",
    ],
}


class FuturesScraper:
    """
    Scrapes futures/outrights markets.

    Futures have 3-10% edge due to varied opinions across books.
    """

    def __init__(self, bookmaker: str, config: Dict[str, Any]):
        self.bookmaker = bookmaker
        self.config = config
        self.selectors = {
            "futures_container": config.get("futures_container_selector", ".futures-market"),
            "market_title": config.get("futures_title_selector", ".market-title"),
            "selection_row": config.get("futures_selection_selector", ".selection-row"),
            "team_name": config.get("futures_team_selector", ".team-name"),
            "odds": config.get("futures_odds_selector", ".odds-value"),
        }

    async def scrape_futures(self, page, sport: str) -> List[MarketOdds]:
        """
        Scrape all futures markets from the current page.
        """
        odds_list: List[MarketOdds] = []

        try:
            containers = await page.query_selector_all(self.selectors["futures_container"])
            logger.info(f"[{self.bookmaker}] Found {len(containers)} futures containers")

            for container in containers:
                try:
                    market_odds = await self._parse_futures_container(container, sport)
                    odds_list.extend(market_odds)
                except Exception as e:
                    logger.warning(f"[{self.bookmaker}] Failed to parse futures: {e}")

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Futures scrape failed: {e}")

        logger.info(f"[{self.bookmaker}] Scraped {len(odds_list)} futures odds")
        return odds_list

    async def _parse_futures_container(self, container, sport: str) -> List[MarketOdds]:
        """Parse a single futures market container."""
        odds_list = []

        # Get market title
        title_elem = await container.query_selector(self.selectors["market_title"])
        market_title = await title_elem.inner_text() if title_elem else "Championship"
        market_type = self._normalize_market_type(market_title)
        event_id = self._generate_event_id(sport, market_type)

        # Estimate expiration (end of season)
        expires_at = self._estimate_expiration(sport)

        # Get all selections
        rows = await container.query_selector_all(self.selectors["selection_row"])

        for row in rows:
            try:
                # Get team/player name
                name_elem = await row.query_selector(self.selectors["team_name"])
                if not name_elem:
                    continue
                selection_name = (await name_elem.inner_text()).strip()

                # Get odds
                odds_elem = await row.query_selector(self.selectors["odds"])
                if not odds_elem:
                    continue
                odds_text = await odds_elem.inner_text()
                odds = self._parse_odds(odds_text)

                if odds and odds > 1.0:
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"futures_{market_type}",
                        bookmaker=self.bookmaker,
                        selection=selection_name,
                        odds_decimal=odds,
                        market_type="future",
                        expires_at=expires_at,
                    ))
            except Exception as e:
                logger.warning(f"Failed to parse futures row: {e}")

        return odds_list

    def _normalize_market_type(self, title: str) -> str:
        """Normalize futures market type."""
        title_lower = title.lower()

        mappings = {
            "super bowl": "super_bowl",
            "nba championship": "championship",
            "nba finals": "championship",
            "world series": "world_series",
            "stanley cup": "stanley_cup",
            "mvp": "mvp",
            "division": "division_winner",
            "conference": "conference_winner",
            "wins": "season_wins",
            "roy": "rookie_of_year",
        }

        for key, value in mappings.items():
            if key in title_lower:
                return value

        return re.sub(r"[^a-z_]", "", title_lower.replace(" ", "_"))

    def _generate_event_id(self, sport: str, market: str) -> str:
        """Generate event ID for futures market."""
        year = datetime.now().year
        return f"futures-{sport}-{market}-{year}"

    def _estimate_expiration(self, sport: str) -> datetime:
        """Estimate when the futures market expires (season end)."""
        now = datetime.utcnow()

        # Approximate season end dates
        season_ends = {
            "nba": datetime(now.year, 6, 30),  # NBA Finals ~June
            "nfl": datetime(now.year, 2, 15),  # Super Bowl ~Feb
            "mlb": datetime(now.year, 11, 5),  # World Series ~Nov
            "nhl": datetime(now.year, 6, 30),  # Stanley Cup ~June
        }

        end = season_ends.get(sport.lower(), now + timedelta(days=180))

        # If season end has passed, move to next year
        if end < now:
            end = end.replace(year=now.year + 1)

        return end

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

    def get_futures_urls(self, sport: str) -> List[str]:
        """Get futures URLs for a sport."""
        base_urls = {
            "fanduel": {
                "nba": "https://sportsbook.fanduel.com/navigation/nba?tab=futures",
                "nfl": "https://sportsbook.fanduel.com/navigation/nfl?tab=futures",
                "mlb": "https://sportsbook.fanduel.com/navigation/mlb?tab=futures",
                "nhl": "https://sportsbook.fanduel.com/navigation/nhl?tab=futures",
            },
            "draftkings": {
                "nba": "https://sportsbook.draftkings.com/leagues/basketball/nba?subcategory=futures",
                "nfl": "https://sportsbook.draftkings.com/leagues/football/nfl?subcategory=futures",
                "mlb": "https://sportsbook.draftkings.com/leagues/baseball/mlb?subcategory=futures",
                "nhl": "https://sportsbook.draftkings.com/leagues/hockey/nhl?subcategory=futures",
            },
            "fanatics": {
                "nba": "https://sportsbook.fanatics.com/sports/basketball/nba/futures",
                "nfl": "https://sportsbook.fanatics.com/sports/football/nfl/futures",
                "mlb": "https://sportsbook.fanatics.com/sports/baseball/mlb/futures",
                "nhl": "https://sportsbook.fanatics.com/sports/hockey/nhl/futures",
            },
        }

        book_urls = base_urls.get(self.bookmaker.lower(), {})
        url = book_urls.get(sport.lower())
        return [url] if url else []

