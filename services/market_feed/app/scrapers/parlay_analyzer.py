"""
Parlay Correlation Analyzer

Detects mispriced parlays and same-game parlays (SGP):
- Compares actual parlay odds to theoretical (uncorrelated) odds
- Identifies correlation errors (e.g., QB passing yards + Team ML)
- Finds 5-15% edge opportunities in SGP markets

Books often fail to properly account for correlations between legs.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


@dataclass
class ParlayLeg:
    """A single leg of a parlay."""
    selection: str
    odds_decimal: float
    market_type: str
    event_id: str


@dataclass
class ParlayAnalysis:
    """Analysis of a parlay's pricing."""
    legs: List[ParlayLeg]
    actual_odds: float
    theoretical_odds: float  # Uncorrelated calculation
    edge_percentage: float
    correlation_factor: float  # >1 = positive correlation, <1 = negative
    is_mispriced: bool


# Known correlation pairs (legs that move together or opposite)
POSITIVE_CORRELATIONS = [
    # If team wins, their star players likely performed well
    ("team_ml", "player_points_over"),
    ("team_ml", "player_yards_over"),
    ("team_ml", "team_total_over"),
    # High-scoring games benefit both teams
    ("total_over", "team_total_over"),
    ("total_over", "player_points_over"),
]

NEGATIVE_CORRELATIONS = [
    # If underdog wins, likely a low-scoring game
    ("underdog_ml", "total_over"),
    # If defense dominates, fewer yards
    ("team_ml", "opponent_yards_over"),
]


class ParlayCorrelationAnalyzer:
    """
    Analyzes parlay odds for correlation mispricings.

    SGPs often have 5-15% edge when books miscalculate correlations.
    """

    def __init__(self, bookmaker: str, config: Dict[str, Any]):
        self.bookmaker = bookmaker
        self.config = config
        self.edge_threshold = config.get("parlay_edge_threshold", 5.0)  # 5%+ is significant
        self.selectors = {
            "sgp_container": config.get("sgp_container_selector", ".sgp-card"),
            "sgp_leg": config.get("sgp_leg_selector", ".sgp-leg"),
            "sgp_odds": config.get("sgp_odds_selector", ".sgp-total-odds"),
            "leg_selection": config.get("leg_selection_selector", ".leg-selection"),
            "leg_odds": config.get("leg_odds_selector", ".leg-odds"),
        }

    def analyze_parlay(self, legs: List[ParlayLeg], actual_odds: float) -> ParlayAnalysis:
        """
        Analyze a parlay for mispricing.

        Returns analysis with edge calculation.
        """
        # Calculate theoretical (uncorrelated) odds
        theoretical = 1.0
        for leg in legs:
            theoretical *= leg.odds_decimal

        # Estimate correlation factor
        correlation = self._estimate_correlation(legs)

        # Adjusted theoretical odds (accounting for correlation)
        # If positively correlated, true odds should be higher
        # If negatively correlated, true odds should be lower
        adjusted_theoretical = theoretical * correlation

        # Calculate edge: (actual_odds / adjusted_theoretical - 1) * 100
        edge = ((actual_odds / adjusted_theoretical) - 1) * 100

        is_mispriced = edge >= self.edge_threshold

        return ParlayAnalysis(
            legs=legs,
            actual_odds=actual_odds,
            theoretical_odds=theoretical,
            edge_percentage=round(edge, 2),
            correlation_factor=round(correlation, 3),
            is_mispriced=is_mispriced,
        )

    def _estimate_correlation(self, legs: List[ParlayLeg]) -> float:
        """
        Estimate correlation factor based on leg types.

        Returns multiplier (>1 = positive correlation, <1 = negative)
        """
        correlation = 1.0

        for i, leg1 in enumerate(legs):
            for leg2 in legs[i+1:]:
                # Check for known correlation pairs
                pair = (leg1.market_type, leg2.market_type)
                reverse_pair = (leg2.market_type, leg1.market_type)

                # Same event legs have stronger correlation
                same_event = leg1.event_id == leg2.event_id
                multiplier = 1.15 if same_event else 1.05

                if pair in POSITIVE_CORRELATIONS or reverse_pair in POSITIVE_CORRELATIONS:
                    correlation *= multiplier
                elif pair in NEGATIVE_CORRELATIONS or reverse_pair in NEGATIVE_CORRELATIONS:
                    correlation /= multiplier

        return correlation

    async def scrape_sgp_odds(self, page, sport: str) -> List[Tuple[List[ParlayLeg], float]]:
        """
        Scrape same-game parlay (SGP) offerings from page.

        Returns list of (legs, actual_odds) tuples.
        """
        results = []

        try:
            containers = await page.query_selector_all(self.selectors["sgp_container"])
            logger.info(f"[{self.bookmaker}] Found {len(containers)} SGP containers")

            for container in containers:
                try:
                    legs = []
                    leg_elems = await container.query_selector_all(self.selectors["sgp_leg"])

                    for leg_elem in leg_elems:
                        sel_elem = await leg_elem.query_selector(self.selectors["leg_selection"])
                        odds_elem = await leg_elem.query_selector(self.selectors["leg_odds"])


                    # Get total SGP odds
                    sgp_odds_elem = await container.query_selector(self.selectors["sgp_odds"])
                    if sgp_odds_elem and len(legs) >= 2:
                        sgp_odds_text = await sgp_odds_elem.inner_text()
                        sgp_odds = self._parse_odds(sgp_odds_text)
                        if sgp_odds and sgp_odds > 1.0:
                            results.append((legs, sgp_odds))

                except Exception as e:
                    logger.warning(f"Failed to parse SGP: {e}")

        except Exception as e:
            logger.error(f"[{self.bookmaker}] SGP scrape failed: {e}")

        return results

    def find_mispriced_parlays(
        self,
        sgp_data: List[Tuple[List[ParlayLeg], float]]
    ) -> List[ParlayAnalysis]:
        """
        Find all mispriced parlays from scraped data.
        """
        mispriced = []

        for legs, actual_odds in sgp_data:
            analysis = self.analyze_parlay(legs, actual_odds)
            if analysis.is_mispriced:
                logger.info(
                    f"[{self.bookmaker}] Found mispriced parlay: "
                    f"{len(legs)} legs @ {actual_odds:.2f} "
                    f"(theoretical: {analysis.theoretical_odds:.2f}, "
                    f"edge: {analysis.edge_percentage:.1f}%)"
                )
                mispriced.append(analysis)

        return mispriced

    def _infer_market_type(self, selection: str) -> str:
        """Infer market type from selection text."""
        sel_lower = selection.lower()

        if "moneyline" in sel_lower or " ml" in sel_lower:
            return "team_ml"
        elif "spread" in sel_lower or any(c in sel_lower for c in ["+", "-"]):
            return "spread"
        elif "over" in sel_lower:
            if "points" in sel_lower:
                return "player_points_over"
            elif "yards" in sel_lower:
                return "player_yards_over"
            else:
                return "total_over"
        elif "under" in sel_lower:
            return "total_under"
        elif "touchdown" in sel_lower or "td" in sel_lower:
            return "player_td"

        return "unknown"

    def _parse_odds(self, odds_text: str) -> Optional[float]:
        """Parse odds from text."""
        import re
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

    def get_sgp_urls(self, sport: str) -> List[str]:
        """Get SGP page URLs."""
        urls = {
            "fanduel": {
                "nba": "https://sportsbook.fanduel.com/navigation/nba?tab=same-game-parlay",
                "nfl": "https://sportsbook.fanduel.com/navigation/nfl?tab=same-game-parlay",
            },
            "draftkings": {
                "nba": "https://sportsbook.draftkings.com/leagues/basketball/nba?subcategory=sgp",
                "nfl": "https://sportsbook.draftkings.com/leagues/football/nfl?subcategory=sgp",
            },
            "fanatics": {
                "nba": "https://sportsbook.fanatics.com/sports/basketball/nba/sgp",
                "nfl": "https://sportsbook.fanatics.com/sports/football/nfl/sgp",
            },
        }
        book_urls = urls.get(self.bookmaker.lower(), {})
        url = book_urls.get(sport.lower())
        return [url] if url else []
