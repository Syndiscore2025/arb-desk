"""
Tests for +EV, Middles, and Promo Converter features.
"""
import pytest
from fastapi.testclient import TestClient
from services.arb_math.app.main import app

client = TestClient(app)


def test_positive_ev_detection():
    """Test +EV detection with real-world scenario."""
    # DK offers Lakers ML at 2.10, FD offers Celtics ML at 2.05
    # No-vig fair odds: Lakers ~1.98, Celtics ~2.03
    # DK Lakers is +EV (2.10 > 1.98)
    payload = {
        "odds": [
            {
                "event_id": "lakers_celtics_123",
                "sport": "basketball_nba",
                "market": "h2h",
                "bookmaker": "draftkings",
                "selection": "Lakers",
                "odds_decimal": 2.10,
                "market_type": "moneyline",
            },
            {
                "event_id": "lakers_celtics_123",
                "sport": "basketball_nba",
                "market": "h2h",
                "bookmaker": "fanduel",
                "selection": "Lakers",
                "odds_decimal": 2.00,
            },
            {
                "event_id": "lakers_celtics_123",
                "sport": "basketball_nba",
                "market": "h2h",
                "bookmaker": "draftkings",
                "selection": "Celtics",
                "odds_decimal": 1.90,
            },
            {
                "event_id": "lakers_celtics_123",
                "sport": "basketball_nba",
                "market": "h2h",
                "bookmaker": "fanduel",
                "selection": "Celtics",
                "odds_decimal": 2.05,
            },
        ]
    }
    
    response = client.post("/positive-ev", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    opportunities = data.get("opportunities", [])
    # Should detect at least one +EV opportunity
    assert len(opportunities) > 0
    
    # Check first opportunity
    opp = opportunities[0]
    assert opp["opportunity_type"] == "positive_ev"
    assert opp["ev_percentage"] > 0
    assert opp["true_probability"] is not None
    assert opp["kelly_fraction"] is not None


def test_middles_detection_spread():
    """Test middle detection for spread bets."""
    # DK: Lakers -3.5, FD: Celtics +5.5 → middle at 4-5 points
    payload = {
        "odds": [
            {
                "event_id": "lakers_celtics_456",
                "sport": "basketball_nba",
                "market": "spreads",
                "bookmaker": "draftkings",
                "selection": "Lakers",
                "odds_decimal": 1.91,
                "market_type": "spread",
                "line": -3.5,
            },
            {
                "event_id": "lakers_celtics_456",
                "sport": "basketball_nba",
                "market": "spreads",
                "bookmaker": "fanduel",
                "selection": "Celtics",
                "odds_decimal": 1.91,
                "market_type": "spread",
                "line": 5.5,
            },
        ]
    }
    
    response = client.post("/middles", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    opportunities = data.get("opportunities", [])
    assert len(opportunities) > 0
    
    opp = opportunities[0]
    assert opp["opportunity_type"] == "middle"
    assert opp["middle_gap"] == 2.0  # 5.5 - 3.5 = 2.0
    assert opp["middle_range"] is not None
    assert len(opp["legs"]) == 2


def test_middles_detection_total():
    """Test middle detection for totals."""
    # DK: Over 210.5, FD: Under 213.5 → middle at 211-213
    payload = {
        "odds": [
            {
                "event_id": "lakers_celtics_789",
                "sport": "basketball_nba",
                "market": "totals",
                "bookmaker": "draftkings",
                "selection": "Over",
                "odds_decimal": 1.91,
                "market_type": "total",
                "line": 210.5,
            },
            {
                "event_id": "lakers_celtics_789",
                "sport": "basketball_nba",
                "market": "totals",
                "bookmaker": "fanduel",
                "selection": "Under",
                "odds_decimal": 1.91,
                "market_type": "total",
                "line": 213.5,
            },
        ]
    }
    
    response = client.post("/middles", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    opportunities = data.get("opportunities", [])
    assert len(opportunities) > 0
    
    opp = opportunities[0]
    assert opp["opportunity_type"] == "middle"
    assert opp["middle_gap"] == 3.0


def test_promo_converter_free_bet_no_stake():
    """Test promo converter for free bet that doesn't return stake."""
    # $50 free bet at +200 (3.00), hedge at -110 (1.91)
    payload = {
        "promo_type": "free_bet",
        "amount": 50.0,
        "odds_decimal": 3.00,
        "hedge_odds_decimal": 1.91,
        "free_bet_returns_stake": False,
    }
    
    response = client.post("/promo-convert", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    assert data["promo_type"] == "free_bet"
    assert data["promo_amount"] == 50.0
    assert data["recommended_hedge_stake"] > 0
    assert data["guaranteed_profit"] > 0
    # For +200 hedged at -110, conversion can be very high (~95%).
    assert data["conversion_rate"] == pytest.approx(0.9529, abs=1e-4)


def test_promo_converter_profit_boost():
    """Test promo converter for profit boost."""
    # $100 bet with 50% profit boost at +150 (2.50), hedge at -110 (1.91)
    payload = {
        "promo_type": "profit_boost",
        "amount": 100.0,
        "boost_percentage": 50.0,
        "odds_decimal": 2.50,
        "hedge_odds_decimal": 1.91,
    }
    
    response = client.post("/promo-convert", json=payload)
    assert response.status_code == 200
    data = response.json()
    
    assert data["promo_type"] == "profit_boost"
    assert data["recommended_hedge_stake"] > 0
    assert data["guaranteed_profit"] > 0

