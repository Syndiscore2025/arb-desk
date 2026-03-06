"""
Tests for Slack notifier: alert control plane, dedupe, quality gates, and bet command handling.
"""
import pytest
import re
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Import the notifier helpers for real integration tests.  We guard against
# import failures so the shallow tests still pass even outside Docker.
# ---------------------------------------------------------------------------
try:
    import services.slack_notifier.app.main as slack_main

    from services.slack_notifier.app.main import (
        _alert_fingerprint,
        _should_suppress_alert,
        _record_alert_sent,
        _record_alert_suppressed,
        _cleanup_stale_dedupe,
        _save_alert_state,
        _load_alert_state,
        _alert_state,
        _alert_stats,
        _alert_dedupe,
        _alert_lifecycle,
        _alert_send_times,
        ALERT_STATE_PATH,
        ALERT_COOLDOWN_SECONDS,
        MIN_ARB_PROFIT_PCT,
        MIN_EV_PCT,
        MIN_MIDDLE_GAP,
    )
    from shared.schemas import ArbOpportunity

    _NOTIFIER_AVAILABLE = True
except Exception:
    _NOTIFIER_AVAILABLE = False


def _make_opp(**overrides) -> "ArbOpportunity":
    """Helper: build a minimal ArbOpportunity for tests."""
    defaults = dict(
        event_id="evt_test_123",
        market="moneyline",
        implied_prob_sum=0.97,
        has_arb=True,
        profit_percentage=2.5,
        opportunity_type="arb",
        legs=[
            {"bookmaker": "fanduel", "selection": "Team A", "odds_decimal": 2.10},
            {"bookmaker": "draftkings", "selection": "Team B", "odds_decimal": 2.05},
        ],
    )
    defaults.update(overrides)
    return ArbOpportunity(**defaults)


def _reset_state():
    """Reset in-memory alert state between tests."""
    _alert_state["enabled"] = True
    _alert_state["disabled_at"] = None
    _alert_state["disabled_by"] = None
    _alert_dedupe.clear()
    _alert_lifecycle.clear()
    _alert_send_times.clear()
    for k in _alert_stats:
        _alert_stats[k] = 0


# ─────────────────────────────────────────────────────────────────────────────
# Alert Control Plane Tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(not _NOTIFIER_AVAILABLE, reason="notifier not importable outside Docker")
class TestAlertControlPlane:
    """Test the alert mute/enable, dedupe, quality gate, and rate-limit logic."""

    def setup_method(self):
        _reset_state()

    # -- Global mute --------------------------------------------------------

    def test_mute_suppresses_alerts(self):
        _alert_state["enabled"] = False
        opp = _make_opp(profit_percentage=5.0)
        reason = _should_suppress_alert(opp)
        assert reason == "alerts_disabled"
        assert _alert_stats["suppressed_muted"] == 1

    def test_unmuted_allows_alerts(self):
        opp = _make_opp(profit_percentage=5.0)
        assert _should_suppress_alert(opp) is None

    # -- Quality gates ------------------------------------------------------

    def test_low_arb_profit_suppressed(self):
        opp = _make_opp(profit_percentage=0.1)
        reason = _should_suppress_alert(opp)
        assert reason is not None
        assert "below" in reason
        assert _alert_stats["suppressed_quality"] == 1

    def test_high_arb_profit_passes(self):
        opp = _make_opp(profit_percentage=2.5)
        assert _should_suppress_alert(opp) is None

    def test_low_ev_suppressed(self):
        opp = _make_opp(
            opportunity_type="positive_ev",
            ev_percentage=0.3,
            profit_percentage=None,
        )
        reason = _should_suppress_alert(opp)
        assert reason is not None
        assert "ev" in reason

    def test_low_middle_gap_suppressed(self):
        opp = _make_opp(
            opportunity_type="middle",
            middle_gap=0.1,
            profit_percentage=None,
        )
        reason = _should_suppress_alert(opp)
        assert reason is not None
        assert "middle" in reason

    # -- Dedupe / cooldown --------------------------------------------------

    def test_dedupe_suppresses_repeat(self):
        opp = _make_opp(profit_percentage=5.0)
        assert _should_suppress_alert(opp) is None
        _record_alert_sent(opp)
        reason = _should_suppress_alert(opp)
        assert reason is not None
        assert "dedupe" in reason

    def test_dedupe_different_opp_passes(self):
        opp1 = _make_opp(profit_percentage=5.0, event_id="evt_A")
        opp2 = _make_opp(profit_percentage=5.0, event_id="evt_B")
        _record_alert_sent(opp1)
        assert _should_suppress_alert(opp2) is None

    # -- Rate limit ---------------------------------------------------------

    def test_rate_limit_blocks_excess(self):
        opp = _make_opp(profit_percentage=5.0)
        # Fill up the rate-limit window
        for i in range(15):
            _alert_send_times.append(datetime.utcnow())
        reason = _should_suppress_alert(opp)
        assert reason is not None
        assert "rate_limit" in reason

    # -- Fingerprint --------------------------------------------------------

    def test_fingerprint_stable(self):
        opp = _make_opp()
        fp1 = _alert_fingerprint(opp)
        fp2 = _alert_fingerprint(opp)
        assert fp1 == fp2

    def test_fingerprint_differs_for_different_event(self):
        opp1 = _make_opp(event_id="A")
        opp2 = _make_opp(event_id="B")
        assert _alert_fingerprint(opp1) != _alert_fingerprint(opp2)

    # -- Lifecycle tracking -------------------------------------------------

    def test_record_sent_creates_entry(self):
        opp = _make_opp()
        _record_alert_sent(opp)
        fp = _alert_fingerprint(opp)
        assert fp in _alert_lifecycle
        assert _alert_lifecycle[fp]["send_count"] == 1

    def test_record_suppressed_creates_entry(self):
        opp = _make_opp()
        _record_alert_suppressed(opp)
        fp = _alert_fingerprint(opp)
        assert fp in _alert_lifecycle
        assert _alert_lifecycle[fp]["suppressed_count"] == 1

    # -- Cleanup ------------------------------------------------------------

    def test_cleanup_stale_dedupe(self):
        opp = _make_opp()
        fp = _alert_fingerprint(opp)
        # Backdate entry so it's stale
        _alert_dedupe[fp] = datetime.utcnow() - timedelta(seconds=99999)
        _cleanup_stale_dedupe()
        assert fp not in _alert_dedupe

    # -- Persistence --------------------------------------------------------

    def test_save_alert_state_writes_json(self, tmp_path, monkeypatch):
        state_path = tmp_path / "alert_state.json"
        monkeypatch.setattr(slack_main, "ALERT_STATE_PATH", str(state_path))

        _alert_state["enabled"] = False
        _alert_state["disabled_by"] = "test"
        opp = _make_opp(profit_percentage=5.0)
        _record_alert_sent(opp)

        assert state_path.exists()
        data = state_path.read_text(encoding="utf-8")
        assert '"enabled": false' in data
        assert '"disabled_by": "test"' in data

    def test_load_alert_state_restores_state(self, tmp_path, monkeypatch):
        state_path = tmp_path / "alert_state.json"
        monkeypatch.setattr(slack_main, "ALERT_STATE_PATH", str(state_path))

        _alert_state["enabled"] = False
        _alert_state["disabled_at"] = "2026-03-05T00:00:00"
        _alert_state["disabled_by"] = "persisted-test"
        opp = _make_opp(profit_percentage=5.0)
        _record_alert_sent(opp)
        fp = _alert_fingerprint(opp)

        _reset_state()
        assert fp not in _alert_dedupe
        _load_alert_state()

        assert _alert_state["enabled"] is False
        assert _alert_state["disabled_by"] == "persisted-test"
        assert fp in _alert_dedupe


# ─────────────────────────────────────────────────────────────────────────────
# Original formatting / parsing tests (kept)
# ─────────────────────────────────────────────────────────────────────────────


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

