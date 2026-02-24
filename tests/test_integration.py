"""
Integration tests for ArbDesk services.

These tests require the Docker services to be running:
    docker compose up -d
"""
import pytest
import httpx
from datetime import datetime


# Service URLs
ODDS_INGEST_URL = "http://localhost:8001"
ARB_MATH_URL = "http://localhost:8002"
DECISION_GATEWAY_URL = "http://localhost:8004"
SLACK_NOTIFIER_URL = "http://localhost:8005"
MARKET_FEED_URL = "http://localhost:8006"


class TestServiceHealth:
    """Test that all services are running and healthy."""

    @pytest.mark.parametrize("service,port", [
        ("odds_ingest", 8001),
        ("arb_math", 8002),
        ("browser_shadow", 8003),
        ("decision_gateway", 8004),
        ("slack_notifier", 8005),
        ("market_feed", 8006),
    ])
    def test_service_health(self, service: str, port: int):
        """Each service should respond to /health."""
        response = httpx.get(f"http://localhost:{port}/health", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == service


class TestArbMathAPI:
    """Test arb_math API endpoints."""

    def test_arbitrage_detection(self):
        """Test arbitrage detection with valid arb scenario."""
        payload = {
            "odds": [
                {
                    "event_id": "TEST_001",
                    "sport": "basketball",
                    "market": "moneyline",
                    "bookmaker": "fanduel",
                    "selection": "TeamA",
                    "odds_decimal": 2.15,
                    "captured_at": datetime.utcnow().isoformat(),
                },
                {
                    "event_id": "TEST_001",
                    "sport": "basketball",
                    "market": "moneyline",
                    "bookmaker": "draftkings",
                    "selection": "TeamB",
                    "odds_decimal": 2.05,
                    "captured_at": datetime.utcnow().isoformat(),
                },
            ]
        }
        
        # Force evaluation output even when no arb meets the default MIN_ARB_PROFIT_PCT filter.
        response = httpx.post(
            f"{ARB_MATH_URL}/arbitrage",
            params={"min_profit_pct": 0.0},
            json=payload,
            timeout=10,
        )
        assert response.status_code == 200
        
        data = response.json()
        assert "opportunities" in data
        assert len(data["opportunities"]) == 1
        
        opp = data["opportunities"][0]
        assert opp["has_arb"] is True
        assert opp["implied_prob_sum"] < 1.0
        assert opp["profit_percentage"] > 0
        assert len(opp["legs"]) == 2

    def test_no_arbitrage_scenario(self):
        """Test when no arbitrage exists."""
        payload = {
            "odds": [
                {
                    "event_id": "TEST_002",
                    "sport": "basketball",
                    "market": "moneyline",
                    "bookmaker": "fanduel",
                    "selection": "TeamA",
                    "odds_decimal": 1.85,
                    "captured_at": datetime.utcnow().isoformat(),
                },
                {
                    "event_id": "TEST_002",
                    "sport": "basketball",
                    "market": "moneyline",
                    "bookmaker": "draftkings",
                    "selection": "TeamB",
                    "odds_decimal": 1.85,
                    "captured_at": datetime.utcnow().isoformat(),
                },
            ]
        }
        
        # Force evaluation output even when no arb meets the default MIN_ARB_PROFIT_PCT filter.
        response = httpx.post(
            f"{ARB_MATH_URL}/arbitrage",
            params={"min_profit_pct": 0.0},
            json=payload,
            timeout=10,
        )
        assert response.status_code == 200
        
        data = response.json()
        opp = data["opportunities"][0]
        assert opp["has_arb"] is False
        assert opp["implied_prob_sum"] > 1.0


class TestOddsIngestPipeline:
    """Test the full odds ingestion pipeline."""

    def test_process_odds_pipeline(self):
        """Test odds flow through the full pipeline."""
        payload = [
            {
                "event_id": "PIPELINE_TEST",
                "sport": "basketball",
                "market": "moneyline",
                "bookmaker": "fanduel",
                "selection": "Heat",
                "odds_decimal": 2.20,
                "captured_at": datetime.utcnow().isoformat(),
            },
            {
                "event_id": "PIPELINE_TEST",
                "sport": "basketball",
                "market": "moneyline",
                "bookmaker": "draftkings",
                "selection": "Bulls",
                "odds_decimal": 2.00,
                "captured_at": datetime.utcnow().isoformat(),
            },
        ]
        
        response = httpx.post(f"{ODDS_INGEST_URL}/process", json=payload, timeout=30)
        assert response.status_code == 200
        
        data = response.json()
        assert data["accepted"] == 2
        assert data["dropped"] == 0


class TestMarketFeedAPI:
    """Test market_feed API endpoints."""

    def test_list_feeds(self):
        """Test listing configured feeds."""
        response = httpx.get(f"{MARKET_FEED_URL}/feeds", timeout=10)
        assert response.status_code == 200
        
        data = response.json()
        assert "feeds" in data
        assert "active_count" in data
        assert "total_count" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

