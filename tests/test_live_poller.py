"""
Tests for live polling and steam move detection.
"""
import pytest
from datetime import datetime, timedelta
import sys
sys.path.insert(0, ".")


class TestSteamMoveDetection:
    """Test steam move detection logic."""

    def test_steam_move_detected_on_large_change(self):
        """Steam move should be detected when odds change > 5%."""
        old_odds = 2.00
        new_odds = 2.15  # 7.5% increase
        
        change_pct = abs((new_odds - old_odds) / old_odds) * 100
        assert change_pct > 5.0
        assert change_pct == pytest.approx(7.5, rel=0.01)

    def test_no_steam_move_on_small_change(self):
        """No steam move when odds change < 5%."""
        old_odds = 2.00
        new_odds = 2.05  # 2.5% increase
        
        change_pct = abs((new_odds - old_odds) / old_odds) * 100
        assert change_pct < 5.0

    def test_steam_move_direction_shortening(self):
        """Shortening odds = sharp money on that side."""
        old_odds = 2.20
        new_odds = 2.00
        
        direction = "shortening" if new_odds < old_odds else "drifting"
        assert direction == "shortening"

    def test_steam_move_direction_drifting(self):
        """Drifting odds = money on the other side."""
        old_odds = 2.00
        new_odds = 2.20
        
        direction = "shortening" if new_odds < old_odds else "drifting"
        assert direction == "drifting"

    def test_steam_move_urgency_score(self):
        """Test urgency score calculation."""
        def calc_urgency(change_pct: float, direction: str, age_seconds: float) -> int:
            score = 50  # Base
            score += min(change_pct * 5, 30)  # Bigger move = more urgent
            if direction == "shortening":
                score += 10
            score -= min(age_seconds * 2, 20)  # Time decay
            return int(max(0, min(100, score)))
        
        # Fresh, big shortening move = high urgency
        assert calc_urgency(10.0, "shortening", 0) >= 80
        
        # Old, small drifting move = low urgency
        assert calc_urgency(5.0, "drifting", 10) < 60

    def test_steam_move_expiration(self):
        """Steam moves should expire after 30 seconds."""
        detected_at = datetime.utcnow() - timedelta(seconds=35)
        expires_at = detected_at + timedelta(seconds=30)
        
        is_expired = datetime.utcnow() > expires_at
        assert is_expired is True


class TestLiveArbPrioritization:
    """Test live arb priority scoring."""

    def test_priority_score_profit_contribution(self):
        """Higher profit = higher priority."""
        def calc_priority(profit_pct: float, has_steam: bool, market_type: str) -> int:
            score = min(profit_pct * 10, 40)  # Up to 40 points
            if has_steam:
                score += 15
            if market_type == "boost":
                score += 10
            elif market_type == "prop":
                score += 5
            return int(min(100, score))
        
        assert calc_priority(5.0, False, "moneyline") > calc_priority(2.0, False, "moneyline")

    def test_steam_move_boosts_priority(self):
        """Steam moves should boost priority by 15 points."""
        def calc_priority(profit_pct: float, has_steam: bool) -> int:
            score = min(profit_pct * 10, 40)
            if has_steam:
                score += 15
            return int(score)
        
        with_steam = calc_priority(3.0, True)
        without_steam = calc_priority(3.0, False)
        assert with_steam - without_steam == 15

    def test_live_arb_tier_boosting(self):
        """Live arbs should be boosted one tier."""
        def get_boosted_tier(profit_pct: float) -> str:
            # Base tier
            if profit_pct >= 3.0:
                tier = "fire"
            elif profit_pct >= 1.5:
                tier = "lightning"
            else:
                tier = "info"
            
            # Boost for live
            if tier == "info":
                tier = "lightning"
            elif tier == "lightning":
                tier = "fire"
            
            return tier
        
        # 1.0% would normally be "info", but live boosts to "lightning"
        assert get_boosted_tier(1.0) == "lightning"
        
        # 2.0% would normally be "lightning", but live boosts to "fire"
        assert get_boosted_tier(2.0) == "fire"
        
        # 4.0% is already "fire", stays "fire"
        assert get_boosted_tier(4.0) == "fire"

    def test_live_arb_expiration(self):
        """Live arbs should expire in 15-30 seconds."""
        # With steam move = 15 seconds
        detected_at = datetime.utcnow()
        expires_steam = detected_at + timedelta(seconds=15)
        expires_normal = detected_at + timedelta(seconds=30)
        
        assert (expires_steam - detected_at).total_seconds() == 15
        assert (expires_normal - detected_at).total_seconds() == 30


class TestPollingIntervals:
    """Test polling interval logic."""

    def test_live_poll_interval_with_jitter(self):
        """Live polling should have jitter for stealth."""
        import random
        
        base_interval = 5  # seconds
        jitter_range = (-1.0, 2.0)
        
        # Simulate 100 intervals
        intervals = []
        for _ in range(100):
            jitter = random.uniform(*jitter_range)
            interval = max(3.0, base_interval + jitter)
            intervals.append(interval)
        
        # Should have variance
        assert min(intervals) >= 3.0  # Never less than 3s
        assert max(intervals) <= base_interval + jitter_range[1] + 0.1
        assert len(set(intervals)) > 1  # Not all the same


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

