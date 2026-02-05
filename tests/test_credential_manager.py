"""
Tests for multi-account credential management.
"""
import pytest
from datetime import datetime, timedelta


class TestCredentialRotation:
    """Test credential rotation logic."""

    def test_rotate_to_next_credential(self):
        """Rotate to next credential in list."""
        credentials = [
            {"username": "user1", "password": "pass1"},
            {"username": "user2", "password": "pass2"},
            {"username": "user3", "password": "pass3"},
        ]
        active_index = 0
        
        # Rotate
        active_index = (active_index + 1) % len(credentials)
        assert active_index == 1
        assert credentials[active_index]["username"] == "user2"
        
        # Rotate again
        active_index = (active_index + 1) % len(credentials)
        assert active_index == 2
        
        # Wrap around
        active_index = (active_index + 1) % len(credentials)
        assert active_index == 0

    def test_cooldown_tracking(self):
        """Track cooldown per credential."""
        cooldowns = {
            "user1": datetime.utcnow() - timedelta(hours=2),
            "user2": datetime.utcnow() - timedelta(minutes=30),
            "user3": datetime.utcnow() - timedelta(minutes=5),
        }
        
        min_cooldown = timedelta(hours=1)
        
        def is_available(username: str) -> bool:
            last_used = cooldowns.get(username)
            if not last_used:
                return True
            return datetime.utcnow() - last_used >= min_cooldown
        
        assert is_available("user1") is True   # 2 hours ago
        assert is_available("user2") is False  # 30 min ago
        assert is_available("user3") is False  # 5 min ago

    def test_find_available_credential(self):
        """Find first available credential."""
        credentials = ["user1", "user2", "user3"]
        cooldowns = {
            "user1": datetime.utcnow() - timedelta(minutes=30),  # Not available
            "user2": datetime.utcnow() - timedelta(hours=2),     # Available
            "user3": datetime.utcnow() - timedelta(minutes=10),  # Not available
        }
        min_cooldown = timedelta(hours=1)
        
        def find_available():
            for cred in credentials:
                last_used = cooldowns.get(cred)
                if not last_used or datetime.utcnow() - last_used >= min_cooldown:
                    return cred
            return None
        
        assert find_available() == "user2"

    def test_no_available_credentials(self):
        """Handle case when no credentials are available."""
        credentials = ["user1", "user2"]
        cooldowns = {
            "user1": datetime.utcnow() - timedelta(minutes=30),
            "user2": datetime.utcnow() - timedelta(minutes=45),
        }
        min_cooldown = timedelta(hours=1)
        
        def find_available():
            for cred in credentials:
                last_used = cooldowns.get(cred)
                if not last_used or datetime.utcnow() - last_used >= min_cooldown:
                    return cred
            return None
        
        assert find_available() is None


class TestLoginFailureTracking:
    """Test login failure tracking and backoff."""

    def test_failure_count_increment(self):
        """Track consecutive login failures."""
        failures = {"user1": 0}
        
        # Simulate failures
        failures["user1"] += 1
        assert failures["user1"] == 1
        
        failures["user1"] += 1
        assert failures["user1"] == 2

    def test_reset_on_success(self):
        """Reset failure count on successful login."""
        failures = {"user1": 5}
        
        # Successful login
        failures["user1"] = 0
        assert failures["user1"] == 0

    def test_disable_after_max_failures(self):
        """Disable credential after max failures."""
        max_failures = 3
        failures = {"user1": 3, "user2": 2}
        
        def is_disabled(username: str) -> bool:
            return failures.get(username, 0) >= max_failures
        
        assert is_disabled("user1") is True
        assert is_disabled("user2") is False

    def test_backoff_increases_with_failures(self):
        """Backoff should increase with failure count."""
        def get_backoff(failure_count: int) -> int:
            base = 60  # 1 minute
            return min(base * (2 ** failure_count), 3600)  # Max 1 hour
        
        assert get_backoff(0) == 60    # 1 min
        assert get_backoff(1) == 120   # 2 min
        assert get_backoff(2) == 240   # 4 min
        assert get_backoff(3) == 480   # 8 min
        assert get_backoff(6) == 3600  # Capped at 1 hour


class TestSingleLoginBookHandling:
    """Test handling of single-login books like DraftKings."""

    def test_detect_forced_logout(self):
        """Detect when book forces logout (another session started)."""
        error_messages = [
            "Session expired",
            "You have been logged out",
            "Another session is active",
            "Please log in again",
        ]
        
        def is_forced_logout(error: str) -> bool:
            indicators = ["session", "logged out", "log in again", "expired"]
            return any(ind in error.lower() for ind in indicators)
        
        assert is_forced_logout("Session expired") is True
        assert is_forced_logout("Another session is active") is True
        assert is_forced_logout("Invalid credentials") is False

    def test_rotate_on_forced_logout(self):
        """Rotate to next credential on forced logout."""
        active_index = 0
        num_credentials = 3
        
        # Forced logout detected, rotate
        active_index = (active_index + 1) % num_credentials
        assert active_index == 1

    def test_wait_before_reusing_credential(self):
        """Wait before reusing a credential that was logged out."""
        last_logout = datetime.utcnow()
        min_wait = timedelta(minutes=15)
        
        # Immediately after logout
        can_reuse = datetime.utcnow() - last_logout >= min_wait
        assert can_reuse is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

