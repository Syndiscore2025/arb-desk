"""
Player Props Scraper

Scrapes player-specific prop bets:
- Points, rebounds, assists (NBA)
- Passing yards, rushing yards, touchdowns (NFL)
- Strikeouts, hits, home runs (MLB)
- Goals, assists, shots (NHL)

Player props typically have 2-5% edge due to less efficient markets.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


# Common player prop categories by sport
PROP_CATEGORIES = {
    "nba": [
        "points", "rebounds", "assists", "steals", "blocks",
        "three_pointers", "pts_rebs_asts", "double_double",
    ],
    "nfl": [
        "passing_yards", "rushing_yards", "receiving_yards",
        "touchdowns", "completions", "interceptions", "receptions",
    ],
    "mlb": [
        "strikeouts", "hits", "home_runs", "rbis",
        "total_bases", "earned_runs", "walks",
    ],
    "nhl": [
        "goals", "assists", "points", "shots", "saves",
    ],
}


class PlayerPropsScraper:
    """
    Scrapes player prop bets from sportsbook pages.

    These are less efficient markets with 2-5% edge potential.
    """

    def __init__(self, bookmaker: str, config: Dict[str, Any]):
        self.bookmaker = bookmaker
        self.config = config
        # Selectors for prop scraping (from extra_config)
        self.selectors = {
            "prop_container": config.get("prop_container_selector", ".player-prop"),
            "player_name": config.get("player_name_selector", ".player-name"),
            "prop_type": config.get("prop_type_selector", ".prop-type"),
            "prop_line": config.get("prop_line_selector", ".prop-line"),
            "over_odds": config.get("over_odds_selector", ".over .odds"),
            "under_odds": config.get("under_odds_selector", ".under .odds"),
            "event_name": config.get("event_name_selector", ".event-name"),
        }

    async def scrape_props(self, page, sport: str) -> List[MarketOdds]:
        """
        Scrape all player props from the current page.

        Args:
            page: Playwright page object
            sport: Sport code (nba, nfl, mlb, nhl)

        Returns:
            List of MarketOdds for each prop selection
        """
        odds_list: List[MarketOdds] = []

        try:
            # Wait for prop containers to load
            await page.wait_for_selector(
                self.selectors["prop_container"],
                timeout=10000
            )

            containers = await page.query_selector_all(self.selectors["prop_container"])
            logger.info(f"[{self.bookmaker}] Found {len(containers)} prop containers")

            for container in containers:
                try:
                    prop_odds = await self._parse_prop_container(container, sport)
                    odds_list.extend(prop_odds)
                except Exception as e:
                    logger.warning(f"[{self.bookmaker}] Failed to parse prop: {e}")
                    continue

        except Exception as e:
            logger.error(f"[{self.bookmaker}] Props scrape failed: {e}")

        logger.info(f"[{self.bookmaker}] Scraped {len(odds_list)} prop odds")
        return odds_list

    async def _parse_prop_container(self, container, sport: str) -> List[MarketOdds]:
        """Parse a single player prop container into MarketOdds."""
        odds_list = []

        # Get player name
        player_elem = await container.query_selector(self.selectors["player_name"])
        player_name = await player_elem.inner_text() if player_elem else "Unknown"
        player_name = player_name.strip()

        # Get prop type (points, rebounds, etc.)
        prop_elem = await container.query_selector(self.selectors["prop_type"])
        prop_type = await prop_elem.inner_text() if prop_elem else "unknown"
        prop_type = self._normalize_prop_type(prop_type)

        # Get line (e.g., 25.5)
        line_elem = await container.query_selector(self.selectors["prop_line"])
        line_text = await line_elem.inner_text() if line_elem else "0"
        line = self._parse_line(line_text)

        # Get event name for event_id
        event_elem = await container.query_selector(self.selectors["event_name"])
        event_name = await event_elem.inner_text() if event_elem else f"{player_name}-game"
        event_id = self._generate_event_id(event_name, player_name, prop_type)

        # Get over odds
        over_elem = await container.query_selector(self.selectors["over_odds"])
        if over_elem:
            over_odds_text = await over_elem.inner_text()
            over_odds = self._parse_odds(over_odds_text)
            if over_odds and over_odds > 1.0:
                odds_list.append(MarketOdds(
                    event_id=event_id,
                    sport=sport,
                    market=f"player_prop_{prop_type}",
                    bookmaker=self.bookmaker,
                    selection=f"{player_name} Over {line}",
                    odds_decimal=over_odds,
                    market_type="prop",
                    player_name=player_name,
                    prop_type=prop_type,
                    line=line,
                ))

        # Get under odds
        under_elem = await container.query_selector(self.selectors["under_odds"])
        if under_elem:
            under_odds_text = await under_elem.inner_text()
            under_odds = self._parse_odds(under_odds_text)
            if under_odds and under_odds > 1.0:
                odds_list.append(MarketOdds(
                    event_id=event_id,
                    sport=sport,
                    market=f"player_prop_{prop_type}",
                    bookmaker=self.bookmaker,
                    selection=f"{player_name} Under {line}",
                    odds_decimal=under_odds,
                    market_type="prop",
                    player_name=player_name,
                    prop_type=prop_type,
                    line=line,
                ))

        return odds_list

    def _normalize_prop_type(self, prop_text: str) -> str:
        """Normalize prop type to standard format."""
        prop_lower = prop_text.lower().strip()

        # Map common variations to standard names
        mappings = {
            "pts": "points", "point": "points",
            "reb": "rebounds", "rebs": "rebounds",
            "ast": "assists", "asts": "assists",
            "stl": "steals",
            "blk": "blocks", "blks": "blocks",
            "3pt": "three_pointers", "threes": "three_pointers",
            "pass yds": "passing_yards", "passing": "passing_yards",
            "rush yds": "rushing_yards", "rushing": "rushing_yards",
            "rec yds": "receiving_yards", "receiving": "receiving_yards",
            "td": "touchdowns", "tds": "touchdowns",
            "k": "strikeouts", "ks": "strikeouts",
            "hr": "home_runs", "hrs": "home_runs",
        }

        for key, value in mappings.items():
            if key in prop_lower:
                return value

        # Remove spaces and special chars
        return re.sub(r"[^a-z_]", "", prop_lower.replace(" ", "_"))

    def _parse_line(self, line_text: str) -> float:
        """Parse prop line from text (e.g., '25.5' or 'Over 25.5')."""
        match = re.search(r"([\d.]+)", line_text)
        if match:
            return float(match.group(1))
        return 0.0

    def _generate_event_id(self, event_name: str, player: str, prop: str) -> str:
        """Generate unique event ID for prop."""
        clean_event = re.sub(r"[^a-zA-Z0-9]", "-", event_name.lower())
        clean_player = re.sub(r"[^a-zA-Z0-9]", "-", player.lower())
        return f"prop-{clean_event}-{clean_player}-{prop}"

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds from text (American or Decimal)."""
        odds_text = odds_text.strip()

        # Check for American odds (+150, -110)
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

        # Try decimal directly
        try:
            decimal = float(re.sub(r"[^\d.]", "", odds_text))
            if decimal > 1.0:
                return round(decimal, 4)
        except ValueError:
            pass

        return None

    def get_prop_urls(self, sport: str) -> List[str]:
        """Get prop-specific URLs for a sport."""
        base_urls = {
            "fanduel": {
                "nba": "https://sportsbook.fanduel.com/navigation/nba?tab=player-props",
                "nfl": "https://sportsbook.fanduel.com/navigation/nfl?tab=player-props",
                "mlb": "https://sportsbook.fanduel.com/navigation/mlb?tab=player-props",
                "nhl": "https://sportsbook.fanduel.com/navigation/nhl?tab=player-props",
            },
            "draftkings": {
                "nba": "https://sportsbook.draftkings.com/leagues/basketball/nba?subcategory=player-props",
                "nfl": "https://sportsbook.draftkings.com/leagues/football/nfl?subcategory=player-props",
                "mlb": "https://sportsbook.draftkings.com/leagues/baseball/mlb?subcategory=player-props",
                "nhl": "https://sportsbook.draftkings.com/leagues/hockey/nhl?subcategory=player-props",
            },
            "fanatics": {
                "nba": "https://sportsbook.fanatics.com/sports/basketball/nba/player-props",
                "nfl": "https://sportsbook.fanatics.com/sports/football/nfl/player-props",
                "mlb": "https://sportsbook.fanatics.com/sports/baseball/mlb/player-props",
                "nhl": "https://sportsbook.fanatics.com/sports/hockey/nhl/player-props",
            },
        }

        book_urls = base_urls.get(self.bookmaker.lower(), {})
        url = book_urls.get(sport.lower())
        return [url] if url else []

