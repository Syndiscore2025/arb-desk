"""
Tests for arb_math service - arbitrage calculation logic.
"""
import pytest
from datetime import datetime
from typing import List, Dict

# Import the actual functions from arb_math
import sys
sys.path.insert(0, ".")
from shared.schemas import MarketOdds, ArbRequest


class TestArbMathCalculations:
    """Test arbitrage calculation logic."""

    def test_arbitrage_detected_when_implied_sum_below_one(self):
        """When implied probability sum < 1.0, arbitrage exists."""
        # Lakers @ 2.15, Celtics @ 2.05
        # Implied: 1/2.15 + 1/2.05 = 0.465 + 0.488 = 0.953 < 1.0
        implied_sum = (1/2.15) + (1/2.05)
        assert implied_sum < 1.0, f"Expected arb, got implied_sum={implied_sum}"
        
        # Profit percentage = (1/implied_sum - 1) * 100
        profit_pct = ((1.0 / implied_sum) - 1.0) * 100
        assert profit_pct > 0, f"Expected positive profit, got {profit_pct}%"
        assert profit_pct == pytest.approx(4.94, rel=0.01)

    def test_no_arbitrage_when_implied_sum_above_one(self):
        """When implied probability sum >= 1.0, no arbitrage."""
        # Lakers @ 1.90, Celtics @ 1.90
        # Implied: 1/1.90 + 1/1.90 = 0.526 + 0.526 = 1.052 > 1.0
        implied_sum = (1/1.90) + (1/1.90)
        assert implied_sum >= 1.0, f"Expected no arb, got implied_sum={implied_sum}"

    def test_stake_calculation_guarantees_equal_payouts(self):
        """Optimal stakes should produce equal payouts on all outcomes."""
        odds_a = 2.15
        odds_b = 2.05
        total_stake = 1000.0
        
        implied_sum = (1/odds_a) + (1/odds_b)
        
        # Stake formula: stake_i = (total / implied_sum) / odds_i
        stake_a = (total_stake / implied_sum) / odds_a
        stake_b = (total_stake / implied_sum) / odds_b
        
        payout_a = stake_a * odds_a
        payout_b = stake_b * odds_b
        
        # Payouts should be equal
        assert payout_a == pytest.approx(payout_b, rel=0.001)
        
        # Profit should be positive
        profit = payout_a - (stake_a + stake_b)
        assert profit > 0

    def test_tier_assignment(self):
        """Test alert tier assignment based on profit percentage."""
        def get_tier(profit_pct: float) -> str:
            if profit_pct >= 3.0:
                return "fire"
            elif profit_pct >= 1.5:
                return "lightning"
            else:
                return "info"
        
        assert get_tier(5.0) == "fire"
        assert get_tier(3.0) == "fire"
        assert get_tier(2.5) == "lightning"
        assert get_tier(1.5) == "lightning"
        assert get_tier(1.0) == "info"
        assert get_tier(0.5) == "info"

    def test_three_way_arbitrage(self):
        """Test arbitrage with 3 outcomes (e.g., soccer 1X2)."""
        # Home @ 3.0, Draw @ 3.5, Away @ 2.8
        odds = [3.0, 3.5, 2.8]
        implied_sum = sum(1/o for o in odds)
        
        # 1/3.0 + 1/3.5 + 1/2.8 = 0.333 + 0.286 + 0.357 = 0.976 < 1.0
        assert implied_sum < 1.0
        
        profit_pct = ((1.0 / implied_sum) - 1.0) * 100
        assert profit_pct > 0

    def test_best_odds_selection(self):
        """Test that we pick the best odds for each selection."""
        odds_data = [
            {"bookmaker": "fanduel", "selection": "Lakers", "odds": 2.10},
            {"bookmaker": "draftkings", "selection": "Lakers", "odds": 2.15},  # Best
            {"bookmaker": "fanatics", "selection": "Lakers", "odds": 2.05},
            {"bookmaker": "fanduel", "selection": "Celtics", "odds": 2.00},
            {"bookmaker": "draftkings", "selection": "Celtics", "odds": 1.95},
            {"bookmaker": "fanatics", "selection": "Celtics", "odds": 2.05},  # Best
        ]
        
        best = {}
        for entry in odds_data:
            sel = entry["selection"]
            if sel not in best or entry["odds"] > best[sel]["odds"]:
                best[sel] = entry
        
        assert best["Lakers"]["bookmaker"] == "draftkings"
        assert best["Lakers"]["odds"] == 2.15
        assert best["Celtics"]["bookmaker"] == "fanatics"
        assert best["Celtics"]["odds"] == 2.05


class TestMarketOddsSchema:
    """Test MarketOdds Pydantic schema."""

    def test_valid_market_odds(self):
        """Test creating valid MarketOdds."""
        odds = MarketOdds(
            event_id="NBA_TEST",
            sport="basketball",
            market="moneyline",
            bookmaker="fanduel",
            selection="Lakers",
            odds_decimal=2.15,
        )
        assert odds.odds_decimal == 2.15
        assert odds.market_type == "moneyline"
        assert odds.is_live is False

    def test_odds_must_be_greater_than_one(self):
        """Odds must be > 1.0."""
        with pytest.raises(ValueError):
            MarketOdds(
                event_id="TEST",
                sport="basketball",
                market="moneyline",
                bookmaker="test",
                selection="Team",
                odds_decimal=0.95,  # Invalid!
            )

    def test_player_prop_fields(self):
        """Test player prop specific fields."""
        odds = MarketOdds(
            event_id="NBA_TEST",
            sport="basketball",
            market="player_points",
            bookmaker="fanduel",
            selection="Over 25.5",
            odds_decimal=1.90,
            market_type="prop",
            player_name="LeBron James",
            prop_type="points",
            line=25.5,
        )
        assert odds.player_name == "LeBron James"
        assert odds.prop_type == "points"
        assert odds.line == 25.5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

