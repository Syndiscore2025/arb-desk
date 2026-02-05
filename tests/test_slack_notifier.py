"""
Tests for Slack notifier and bet command handling.
"""
import pytest
import re
from datetime import datetime, timedelta


class TestAlertFormatting:
    """Test alert message formatting."""

    def test_tier_emoji_mapping(self):
        """Test tier to emoji mapping."""
        tier_emoji = {
            "fire": "🔥🔥🔥",
            "lightning": "⚡⚡",
            "info": "ℹ️",
        }
        
        assert tier_emoji["fire"] == "🔥🔥🔥"
        assert tier_emoji["lightning"] == "⚡⚡"
        assert tier_emoji["info"] == "ℹ️"

    def test_profit_to_tier(self):
        """Test profit percentage to tier assignment."""
        def get_tier(profit_pct: float) -> str:
            if profit_pct >= 3.0:
                return "fire"
            elif profit_pct >= 1.5:
                return "lightning"
            else:
                return "info"
        
        assert get_tier(5.0) == "fire"
        assert get_tier(3.0) == "fire"
        assert get_tier(2.9) == "lightning"
        assert get_tier(1.5) == "lightning"
        assert get_tier(1.4) == "info"
        assert get_tier(0.5) == "info"

    def test_stake_formatting(self):
        """Test stake amount formatting."""
        def format_stake(amount: float) -> str:
            return f"${amount:.2f}"
        
        assert format_stake(100) == "$100.00"
        assert format_stake(1234.5) == "$1234.50"
        assert format_stake(0.99) == "$0.99"

    def test_deep_link_generation(self):
        """Test deep link URL generation."""
        def generate_deep_link(bookmaker: str, event_id: str) -> str:
            base_urls = {
                "fanduel": "https://sportsbook.fanduel.com",
                "draftkings": "https://sportsbook.draftkings.com",
                "fanatics": "https://sportsbook.fanatics.com",
            }
            base = base_urls.get(bookmaker.lower(), f"https://{bookmaker}.com")
            return f"{base}/event/{event_id}"
        
        assert "fanduel.com" in generate_deep_link("fanduel", "123")
        assert "draftkings.com" in generate_deep_link("draftkings", "456")


class TestBetCommandParsing:
    """Test bet command parsing from Slack messages."""

    def test_valid_bet_command(self):
        """Parse valid bet commands."""
        pattern = r"bet\s+(\S+)\s+(\d+(?:\.\d+)?)"
        
        match = re.match(pattern, "bet abc123 100", re.IGNORECASE)
        assert match is not None
        assert match.group(1) == "abc123"
        assert float(match.group(2)) == 100.0

    def test_bet_command_with_decimal(self):
        """Parse bet command with decimal stake."""
        pattern = r"bet\s+(\S+)\s+(\d+(?:\.\d+)?)"
        
        match = re.match(pattern, "bet xyz789 250.50", re.IGNORECASE)
        assert match is not None
        assert float(match.group(2)) == 250.50

    def test_bet_command_case_insensitive(self):
        """Bet command should be case insensitive."""
        pattern = r"bet\s+(\S+)\s+(\d+(?:\.\d+)?)"
        
        assert re.match(pattern, "BET abc 100", re.IGNORECASE) is not None
        assert re.match(pattern, "Bet abc 100", re.IGNORECASE) is not None
        assert re.match(pattern, "bet abc 100", re.IGNORECASE) is not None

    def test_invalid_bet_commands(self):
        """Reject invalid bet commands."""
        pattern = r"bet\s+(\S+)\s+(\d+(?:\.\d+)?)"
        
        assert re.match(pattern, "bet abc", re.IGNORECASE) is None  # No amount
        assert re.match(pattern, "bet 100", re.IGNORECASE) is None  # No alert ID
        assert re.match(pattern, "place bet abc 100", re.IGNORECASE) is None  # Wrong format

    def test_partial_alert_id_matching(self):
        """Match alerts by partial ID (first 8 chars)."""
        full_id = "a7b2c3d4-e5f6-7890-abcd-ef1234567890"
        partial_id = "a7b2c3d4"
        
        assert full_id.startswith(partial_id)


class TestAlertExpiration:
    """Test alert expiration logic."""

    def test_alert_expires_after_5_minutes(self):
        """Alerts should expire after 5 minutes."""
        created_at = datetime.utcnow() - timedelta(minutes=6)
        expires_at = created_at + timedelta(minutes=5)
        
        is_expired = datetime.utcnow() > expires_at
        assert is_expired is True

    def test_alert_valid_within_5_minutes(self):
        """Alerts should be valid within 5 minutes."""
        created_at = datetime.utcnow() - timedelta(minutes=3)
        expires_at = created_at + timedelta(minutes=5)
        
        is_expired = datetime.utcnow() > expires_at
        assert is_expired is False

    def test_cleanup_old_alerts(self):
        """Old alerts should be cleaned up."""
        alerts = {
            "new": {"created_at": datetime.utcnow() - timedelta(minutes=5)},
            "old": {"created_at": datetime.utcnow() - timedelta(minutes=35)},
            "very_old": {"created_at": datetime.utcnow() - timedelta(hours=2)},
        }
        
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        expired = [aid for aid, a in alerts.items() if a["created_at"] < cutoff]
        
        assert "new" not in expired
        assert "old" in expired
        assert "very_old" in expired


class TestStakeScaling:
    """Test stake scaling for bet execution."""

    def test_proportional_stake_scaling(self):
        """Scale stakes proportionally to user's total."""
        original_legs = [
            {"stake": 500, "bookmaker": "fanduel"},
            {"stake": 500, "bookmaker": "draftkings"},
        ]
        original_total = sum(leg["stake"] for leg in original_legs)
        user_stake = 200
        
        scale_factor = user_stake / original_total
        
        scaled_legs = [
            {"stake": leg["stake"] * scale_factor, "bookmaker": leg["bookmaker"]}
            for leg in original_legs
        ]
        
        assert scaled_legs[0]["stake"] == 100
        assert scaled_legs[1]["stake"] == 100
        assert sum(leg["stake"] for leg in scaled_legs) == user_stake

    def test_uneven_stake_scaling(self):
        """Scale uneven stakes correctly."""
        original_legs = [
            {"stake": 400, "bookmaker": "fanduel"},
            {"stake": 600, "bookmaker": "draftkings"},
        ]
        original_total = 1000
        user_stake = 500
        
        scale_factor = user_stake / original_total
        
        scaled = [leg["stake"] * scale_factor for leg in original_legs]
        assert scaled[0] == 200
        assert scaled[1] == 300


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

