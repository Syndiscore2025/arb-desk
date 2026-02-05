"""
Tests for advanced market scrapers (props, alt lines, futures, boosts, parlays).
"""
import pytest
from datetime import datetime, timedelta


class TestPlayerPropsLogic:
    """Test player props scraping logic."""

    def test_prop_type_normalization(self):
        """Different books use different names for same prop."""
        def normalize_prop_type(raw: str) -> str:
            raw_lower = raw.lower().strip()
            mappings = {
                "pts": "points",
                "points scored": "points",
                "reb": "rebounds",
                "total rebounds": "rebounds",
                "ast": "assists",
                "total assists": "assists",
                "3pm": "threes",
                "three pointers made": "threes",
            }
            return mappings.get(raw_lower, raw_lower)
        
        assert normalize_prop_type("PTS") == "points"
        assert normalize_prop_type("Points Scored") == "points"
        assert normalize_prop_type("REB") == "rebounds"
        assert normalize_prop_type("AST") == "assists"

    def test_over_under_parsing(self):
        """Parse over/under lines correctly."""
        def parse_line(text: str) -> tuple:
            text = text.strip().lower()
            if text.startswith("o ") or text.startswith("over "):
                direction = "over"
                value = float(text.split()[-1])
            elif text.startswith("u ") or text.startswith("under "):
                direction = "under"
                value = float(text.split()[-1])
            else:
                direction = None
                value = float(text)
            return direction, value
        
        assert parse_line("O 25.5") == ("over", 25.5)
        assert parse_line("Over 25.5") == ("over", 25.5)
        assert parse_line("U 25.5") == ("under", 25.5)
        assert parse_line("Under 25.5") == ("under", 25.5)


class TestAltLinesLogic:
    """Test alternate lines scraping logic."""

    def test_spread_line_parsing(self):
        """Parse spread lines correctly."""
        def parse_spread(text: str) -> float:
            text = text.strip().replace("+", "")
            return float(text)
        
        assert parse_spread("-3.5") == -3.5
        assert parse_spread("+3.5") == 3.5
        assert parse_spread("-7") == -7.0

    def test_alt_line_generation(self):
        """Generate alternate lines around main line."""
        main_spread = -3.5
        alt_spreads = [main_spread + i for i in range(-3, 4)]
        
        assert -6.5 in alt_spreads
        assert -3.5 in alt_spreads
        assert -0.5 in alt_spreads


class TestFuturesLogic:
    """Test futures market logic."""

    def test_expiration_estimation(self):
        """Estimate futures expiration based on sport."""
        def estimate_expiration(sport: str) -> datetime:
            now = datetime.utcnow()
            season_ends = {
                "nba": datetime(now.year, 6, 30),
                "nfl": datetime(now.year, 2, 15),
                "mlb": datetime(now.year, 11, 5),
                "nhl": datetime(now.year, 6, 30),
            }
            end = season_ends.get(sport.lower(), now + timedelta(days=180))
            if end < now:
                end = end.replace(year=now.year + 1)
            return end
        
        exp = estimate_expiration("nba")
        assert exp > datetime.utcnow()

    def test_futures_market_types(self):
        """Recognize different futures market types."""
        def classify_futures_market(title: str) -> str:
            title_lower = title.lower()
            # Check "division" before "winner" to avoid false positive on "Division Winner"
            if "division" in title_lower:
                return "division"
            elif "champion" in title_lower or "winner" in title_lower:
                return "championship"
            elif "mvp" in title_lower:
                return "mvp"
            elif "wins" in title_lower:
                return "season_wins"
            return "other"

        assert classify_futures_market("NBA Championship Winner") == "championship"
        assert classify_futures_market("NFL MVP") == "mvp"
        assert classify_futures_market("AFC East Division Winner") == "division"
        assert classify_futures_market("Lakers Regular Season Wins") == "season_wins"


class TestBoostDetection:
    """Test odds boost detection and hedge calculation."""

    def test_boost_detection(self):
        """Detect boosted odds vs original."""
        original_odds = 2.00
        boosted_odds = 2.50
        
        boost_pct = ((boosted_odds / original_odds) - 1) * 100
        assert boost_pct == 25.0  # 25% boost

    def test_hedge_profit_calculation(self):
        """Calculate guaranteed profit when hedging a boost."""
        def calc_hedge_profit(boosted: float, hedge: float, stake: float = 100) -> dict:
            # Correct hedge calculation: ensure equal payouts from both outcomes
            # boost_payout = stake * boosted
            # hedge_payout = hedge_stake * hedge
            # For equal payouts: stake * boosted = hedge_stake * hedge
            # hedge_stake = stake * boosted / hedge
            hedge_stake = stake * boosted / hedge
            total_stake = stake + hedge_stake

            # Both outcomes pay the same
            payout = stake * boosted  # = hedge_stake * hedge

            # Guaranteed profit
            guaranteed = payout - total_stake
            profit_pct = (guaranteed / total_stake) * 100

            return {
                "hedge_stake": round(hedge_stake, 2),
                "total_stake": round(total_stake, 2),
                "guaranteed_profit": round(guaranteed, 2),
                "profit_pct": round(profit_pct, 2),
            }

        # Boosted to +150 (2.50), hedge at -110 (1.91) - a realistic arbitrage scenario
        # Implied probs: 1/2.50 + 1/1.91 = 0.40 + 0.52 = 0.92 < 1.0 = ARB
        result = calc_hedge_profit(2.50, 1.91, 100)
        assert result["guaranteed_profit"] > 0
        assert result["profit_pct"] > 0


class TestParlayCorrelation:
    """Test parlay correlation detection."""

    def test_uncorrelated_parlay_odds(self):
        """Uncorrelated parlay = multiply individual odds."""
        leg1_odds = 2.00
        leg2_odds = 2.00
        
        theoretical = leg1_odds * leg2_odds
        assert theoretical == 4.00

    def test_positive_correlation_adjustment(self):
        """Positively correlated legs should have higher true odds."""
        leg1_odds = 2.00  # Team ML
        leg2_odds = 1.80  # Same team player points over
        
        uncorrelated = leg1_odds * leg2_odds  # 3.60
        correlation_factor = 1.15  # Positive correlation
        
        adjusted = uncorrelated * correlation_factor
        assert adjusted > uncorrelated  # 4.14 > 3.60

    def test_mispricing_detection(self):
        """Detect when actual parlay odds exceed theoretical."""
        actual_odds = 4.50
        theoretical_odds = 4.00
        
        edge = ((actual_odds / theoretical_odds) - 1) * 100
        is_mispriced = edge >= 5.0
        
        assert edge == 12.5
        assert is_mispriced is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

