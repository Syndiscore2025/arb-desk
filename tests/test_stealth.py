"""
Tests for stealth browser automation logic.
"""
import pytest
import random


class TestFingerprintGeneration:
    """Test browser fingerprint generation."""

    def test_viewport_randomization(self):
        """Viewport should be randomized within realistic bounds."""
        viewports = [
            (1920, 1080), (1366, 768), (1536, 864),
            (1440, 900), (1280, 720), (1600, 900),
        ]
        
        selected = random.choice(viewports)
        assert 1280 <= selected[0] <= 1920
        assert 720 <= selected[1] <= 1080

    def test_user_agent_variety(self):
        """Should have multiple user agent options."""
        user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Firefox/121.0",
        ]
        
        assert len(user_agents) >= 3
        assert all("Mozilla" in ua for ua in user_agents)

    def test_hardware_concurrency_realistic(self):
        """Hardware concurrency should be realistic (2-16 cores)."""
        cores = random.choice([2, 4, 6, 8, 12, 16])
        assert 2 <= cores <= 16

    def test_device_memory_realistic(self):
        """Device memory should be realistic (4-32 GB)."""
        memory = random.choice([4, 8, 16, 32])
        assert memory in [4, 8, 16, 32]


class TestHumanBehaviorSimulation:
    """Test human-like behavior simulation."""

    def test_typing_delay_variance(self):
        """Typing delays should vary between keystrokes."""
        def get_typing_delay() -> float:
            base = 0.05  # 50ms base
            variance = random.uniform(-0.02, 0.05)
            return max(0.03, base + variance)
        
        delays = [get_typing_delay() for _ in range(100)]
        
        # Should have variance
        assert len(set(delays)) > 1
        # Should be realistic (30-100ms)
        assert all(0.03 <= d <= 0.15 for d in delays)

    def test_typo_simulation(self):
        """Occasionally make typos and correct them."""
        def should_make_typo(typo_rate: float = 0.02) -> bool:
            return random.random() < typo_rate
        
        # Over 1000 chars, should have ~20 typos at 2% rate
        typos = sum(1 for _ in range(1000) if should_make_typo())
        assert 5 < typos < 50  # Reasonable range

    def test_mouse_movement_not_linear(self):
        """Mouse movements should follow curves, not straight lines."""
        def bezier_point(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
            """Cubic bezier curve point."""
            return (
                (1-t)**3 * p0 +
                3 * (1-t)**2 * t * p1 +
                3 * (1-t) * t**2 * p2 +
                t**3 * p3
            )
        
        # Generate curve from (0,0) to (100,100)
        points = []
        for i in range(11):
            t = i / 10
            x = bezier_point(t, 0, 30, 70, 100)
            y = bezier_point(t, 0, 60, 40, 100)
            points.append((x, y))
        
        # Should not be a straight line (y != x for middle points)
        middle_points = points[3:8]
        assert not all(abs(p[0] - p[1]) < 1 for p in middle_points)

    def test_scroll_pattern_variance(self):
        """Scroll amounts should vary."""
        def get_scroll_amount() -> int:
            base = 300
            variance = random.randint(-100, 150)
            return base + variance
        
        scrolls = [get_scroll_amount() for _ in range(50)]
        assert len(set(scrolls)) > 1
        assert all(100 <= s <= 500 for s in scrolls)


class TestBanDetection:
    """Test ban/challenge detection."""

    def test_cloudflare_detection(self):
        """Detect Cloudflare challenge pages."""
        def is_cloudflare_challenge(html: str) -> bool:
            indicators = [
                "cf-browser-verification",
                "cloudflare",
                "checking your browser",
                "ray id",
            ]
            html_lower = html.lower()
            return any(ind in html_lower for ind in indicators)
        
        cf_html = "<html><body>Checking your browser... Ray ID: abc123</body></html>"
        normal_html = "<html><body>Welcome to our sportsbook!</body></html>"
        
        assert is_cloudflare_challenge(cf_html) is True
        assert is_cloudflare_challenge(normal_html) is False

    def test_datadome_detection(self):
        """Detect DataDome challenge pages."""
        def is_datadome_challenge(html: str) -> bool:
            indicators = ["datadome", "dd.js", "captcha-delivery"]
            html_lower = html.lower()
            return any(ind in html_lower for ind in indicators)
        
        dd_html = "<html><script src='dd.js'></script></html>"
        assert is_datadome_challenge(dd_html) is True

    def test_rate_limit_detection(self):
        """Detect rate limiting responses."""
        def is_rate_limited(status_code: int, html: str) -> bool:
            if status_code == 429:
                return True
            rate_limit_phrases = ["too many requests", "rate limit", "slow down"]
            return any(phrase in html.lower() for phrase in rate_limit_phrases)
        
        assert is_rate_limited(429, "") is True
        assert is_rate_limited(200, "Too many requests, please slow down") is True
        assert is_rate_limited(200, "Welcome!") is False


class TestExponentialBackoff:
    """Test exponential backoff for retries."""

    def test_backoff_increases(self):
        """Backoff delay should increase exponentially."""
        def get_backoff(attempt: int, base: int = 30) -> int:
            return min(base * (2 ** attempt), 900)  # Max 15 min
        
        assert get_backoff(0) == 30   # 30s
        assert get_backoff(1) == 60   # 1m
        assert get_backoff(2) == 120  # 2m
        assert get_backoff(3) == 240  # 4m
        assert get_backoff(4) == 480  # 8m
        assert get_backoff(5) == 900  # 15m (capped)

    def test_backoff_with_jitter(self):
        """Add jitter to prevent thundering herd."""
        def get_backoff_with_jitter(attempt: int) -> float:
            base = 30 * (2 ** attempt)
            jitter = random.uniform(0, base * 0.1)
            return min(base + jitter, 900)
        
        delays = [get_backoff_with_jitter(2) for _ in range(100)]
        assert len(set(delays)) > 1  # Should have variance


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

