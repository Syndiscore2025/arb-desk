"""
Tests for prediction market adapters (Polymarket, Kalshi).
"""
import pytest
from datetime import datetime


class TestPolymarketLogic:
    """Test Polymarket adapter logic."""

    def test_probability_to_decimal_odds(self):
        """Convert probability (0-1) to decimal odds."""
        def prob_to_odds(probability: float) -> float:
            if probability <= 0 or probability >= 1:
                return None
            return round(1 / probability, 4)
        
        assert prob_to_odds(0.50) == 2.0
        assert prob_to_odds(0.25) == 4.0
        assert prob_to_odds(0.75) == pytest.approx(1.3333, rel=0.01)
        assert prob_to_odds(0.10) == 10.0

    def test_invalid_probability_handling(self):
        """Handle edge case probabilities."""
        def prob_to_odds(probability: float) -> float:
            if probability <= 0 or probability >= 1:
                return None
            return round(1 / probability, 4)
        
        assert prob_to_odds(0) is None
        assert prob_to_odds(1) is None
        assert prob_to_odds(-0.5) is None

    def test_market_category_filtering(self):
        """Filter markets by category."""
        categories = ["sports", "politics", "crypto", "entertainment"]
        
        def is_sports_market(category: str) -> bool:
            return category.lower() in ["sports", "nba", "nfl", "mlb", "nhl"]
        
        assert is_sports_market("sports") is True
        assert is_sports_market("NBA") is True
        assert is_sports_market("politics") is False


class TestKalshiLogic:
    """Test Kalshi adapter logic."""

    def test_cents_to_decimal_odds(self):
        """Convert Kalshi cents (0-100) to decimal odds."""
        def cents_to_odds(cents: int) -> float:
            if cents <= 0 or cents >= 100:
                return None
            probability = cents / 100
            return round(1 / probability, 4)
        
        assert cents_to_odds(50) == 2.0
        assert cents_to_odds(25) == 4.0
        assert cents_to_odds(75) == pytest.approx(1.3333, rel=0.01)

    def test_bid_ask_midpoint(self):
        """Calculate midpoint from bid/ask spread."""
        def get_midpoint(bid: int, ask: int) -> float:
            return (bid + ask) / 2 / 100  # Convert to probability
        
        assert get_midpoint(48, 52) == 0.50
        assert get_midpoint(60, 70) == 0.65

    def test_spread_too_wide(self):
        """Detect when bid/ask spread is too wide for arb."""
        def spread_pct(bid: int, ask: int) -> float:
            return (ask - bid) / ((bid + ask) / 2) * 100
        
        # Tight spread = good
        assert spread_pct(48, 52) < 10
        
        # Wide spread = bad for arb
        assert spread_pct(30, 50) > 10


class TestCrossMarketArbitrage:
    """Test cross-market arbitrage detection."""

    def test_sportsbook_vs_prediction_market_arb(self):
        """Detect arb between sportsbook and prediction market."""
        # Sportsbook: Lakers to win @ 2.10 (implied 47.6%)
        # Polymarket: Lakers to win @ 55% probability (1.82 odds)
        
        sb_odds = 2.10
        sb_implied = 1 / sb_odds  # 0.476
        
        pred_prob = 0.55
        pred_odds = 1 / pred_prob  # 1.82
        
        # Arb exists if: sb_implied + (1 - pred_prob) < 1
        # 0.476 + 0.45 = 0.926 < 1.0 = ARB!
        arb_sum = sb_implied + (1 - pred_prob)
        has_arb = arb_sum < 0.98  # 2% edge threshold
        
        assert has_arb is True
        
        profit_pct = ((1 / arb_sum) - 1) * 100
        assert profit_pct > 0

    def test_event_matching(self):
        """Match events across platforms by name similarity."""
        def match_score(name1: str, name2: str) -> float:
            words1 = set(name1.lower().split())
            words2 = set(name2.lower().split())
            intersection = words1 & words2
            union = words1 | words2
            return len(intersection) / len(union) if union else 0
        
        # Good match
        score1 = match_score("Lakers vs Celtics", "Los Angeles Lakers vs Boston Celtics")
        assert score1 > 0.3
        
        # Bad match
        score2 = match_score("Lakers vs Celtics", "Heat vs Bulls")
        assert score2 <= 0.2


class TestCLVCalculation:
    """Test Closing Line Value calculation."""

    def test_positive_clv(self):
        """Positive CLV = you beat the market."""
        odds_at_bet = 2.20
        closing_odds = 2.00

        clv = ((odds_at_bet / closing_odds) - 1) * 100
        assert clv == pytest.approx(10.0)  # 10% CLV

    def test_negative_clv(self):
        """Negative CLV = market moved against you."""
        odds_at_bet = 2.00
        closing_odds = 2.20
        
        clv = ((odds_at_bet / closing_odds) - 1) * 100
        assert clv == pytest.approx(-9.09, rel=0.01)

    def test_clv_tracking_over_time(self):
        """Track CLV across multiple bets."""
        bets = [
            {"odds_at_bet": 2.20, "closing": 2.00},  # +10%
            {"odds_at_bet": 1.90, "closing": 2.00},  # -5%
            {"odds_at_bet": 2.50, "closing": 2.30},  # +8.7%
        ]
        
        clvs = [((b["odds_at_bet"] / b["closing"]) - 1) * 100 for b in bets]
        avg_clv = sum(clvs) / len(clvs)
        
        assert avg_clv > 0  # Positive average = profitable long-term


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

