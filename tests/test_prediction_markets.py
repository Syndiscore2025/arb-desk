"""
Tests for prediction market adapters (Polymarket, Kalshi).
"""
import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from shared.schemas import MarketOdds
from services.market_feed.app.adapters.prediction_markets import (
    PolymarketAdapter,
    KalshiAdapter,
    PredictionMarketEventUnifier,
)
from services.market_feed.app.prediction_market_diagnostics import (
    categorize_prediction_market_titles,
)
from services.market_feed.app.prediction_market_runtime import PredictionMarketRuntimeStore


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _make_prediction_odds(event_id: str, bookmaker: str, selection: str = "Yes") -> MarketOdds:
    return MarketOdds(
        event_id=event_id,
        sport="prediction",
        market=f"Market {event_id}",
        bookmaker=bookmaker,
        selection=selection,
        odds_decimal=2.0,
        market_type="prediction",
    )


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

    def test_fetch_markets_rotates_offset_between_calls(self, monkeypatch):
        adapter = PolymarketAdapter()
        adapter.page_limit = 2
        adapter.max_pages = 1

        offsets = []
        payloads = {
            0: {"markets": [{"id": "m1"}, {"id": "m2"}]},
            2: {"markets": [{"id": "m3"}]},
        }

        async def fake_get(_url, params=None, **_kwargs):
            offset = params.get("offset", 0)
            offsets.append(offset)
            return _StubResponse(payloads.get(offset, {"markets": []}))

        monkeypatch.setattr(adapter.client, "get", fake_get)
        monkeypatch.setattr(
            adapter,
            "_parse_market",
            lambda market: [_make_prediction_odds(f"poly-{market['id']}", "polymarket")],
        )

        async def exercise():
            first = await adapter.fetch_markets()
            first_offset = adapter._next_offset
            second = await adapter.fetch_markets()
            second_offset = adapter._next_offset
            await adapter.close()
            return first, first_offset, second, second_offset

        first, first_offset, second, second_offset = asyncio.run(exercise())

        assert offsets == [0, 2]
        assert len(first) == 2
        assert len(second) == 1
        assert first_offset == 2
        assert second_offset == 0

    def test_fetch_markets_persists_offset_between_instances(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PREDICTION_MARKET_STATE_DIR", str(tmp_path))

        adapter = PolymarketAdapter()
        adapter.page_limit = 2
        adapter.max_pages = 1

        async def fake_get(_url, params=None, **_kwargs):
            assert params.get("offset") == 0
            return _StubResponse({"markets": [{"id": "m1"}, {"id": "m2"}]})

        monkeypatch.setattr(adapter.client, "get", fake_get)
        monkeypatch.setattr(
            adapter,
            "_parse_market",
            lambda market: [_make_prediction_odds(f"poly-{market['id']}", "polymarket")],
        )

        async def exercise():
            await adapter.fetch_markets()
            await adapter.close()

        asyncio.run(exercise())

        reloaded = PolymarketAdapter()
        try:
            assert reloaded._next_offset == 2
            snapshot = reloaded.runtime_state_snapshot()
            assert snapshot["next_offset"] == 2
            assert snapshot["last_fetch_count"] == 2
        finally:
            asyncio.run(reloaded.close())


class TestKalshiLogic:
    """Test Kalshi adapter logic."""

    def test_dedupe_keeps_yes_and_no_for_same_event(self):
        odds = [
            MarketOdds(
                event_id="kalshi-abc",
                sport="prediction",
                market="Will X happen?",
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=2.0,
                market_type="prediction",
            ),
            # Duplicate yes row (should be removed)
            MarketOdds(
                event_id="kalshi-abc",
                sport="prediction",
                market="Will X happen?",
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=2.1,
                market_type="prediction",
            ),
            # No row for same event (must be preserved)
            MarketOdds(
                event_id="kalshi-abc",
                sport="prediction",
                market="Will X happen?",
                bookmaker="kalshi",
                selection="No",
                odds_decimal=1.9,
                market_type="prediction",
            ),
        ]

        deduped = KalshiAdapter._dedupe_odds_keep_sides(odds)
        assert len(deduped) == 2
        assert {(o.event_id, o.selection) for o in deduped} == {("kalshi-abc", "Yes"), ("kalshi-abc", "No")}

    def test_weather_event_ticker_is_not_excluded_from_non_sports_discovery(self):
        assert KalshiAdapter._is_excluded_non_sports_event_ticker("WEATHER-NYC-20260115") is False

    def test_sports_event_ticker_is_excluded_from_non_sports_discovery(self):
        assert KalshiAdapter._is_excluded_non_sports_event_ticker("KXNBA-LAL-BOS-20260115") is True

    def test_open_weather_market_is_non_sports_scan_candidate(self):
        market = {
            "ticker": "WEATHER-NYC-20260115-HIGH-TEMP",
            "event_ticker": "WEATHER-NYC-20260115",
            "status": "open",
            "market_type": "binary",
        }

        assert KalshiAdapter._is_non_sports_market_candidate(market) is True

    def test_initialized_weather_market_is_not_non_sports_scan_candidate(self):
        market = {
            "ticker": "WEATHER-NYC-20260115-HIGH-TEMP",
            "event_ticker": "WEATHER-NYC-20260115",
            "status": "initialized",
            "market_type": "binary",
        }

        assert KalshiAdapter._is_non_sports_market_candidate(market) is False

    def test_sports_market_is_not_non_sports_scan_candidate(self):
        market = {
            "ticker": "KXNBA-LAL-BOS-20260115",
            "event_ticker": "NBAASST-LAL-BOS-20260115",
            "status": "open",
            "market_type": "binary",
        }

        assert KalshiAdapter._is_non_sports_market_candidate(market) is False

    def test_global_open_fetch_rotates_cursor_between_calls(self, monkeypatch):
        monkeypatch.setenv("KALSHI_GLOBAL_MARKETS_PAGE_LIMIT", "2")
        monkeypatch.setenv("KALSHI_GLOBAL_MAX_PAGES", "1")

        adapter = KalshiAdapter()
        cursors = []
        payloads = {
            None: {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "next-a"},
            "next-a": {"markets": [{"ticker": "C"}]},
        }

        async def fake_get_json(path, params=None):
            assert path == "/markets"
            cursor = (params or {}).get("cursor")
            cursors.append(cursor)
            return payloads.get(cursor, {"markets": []})

        monkeypatch.setattr(adapter, "_get_json", fake_get_json)
        monkeypatch.setattr(
            adapter,
            "_parse_market",
            lambda market: [_make_prediction_odds(f"kalshi-{market['ticker']}", "kalshi")],
        )

        async def exercise():
            first = await adapter._fetch_open_markets_global()
            first_cursor = adapter._global_markets_cursor
            second = await adapter._fetch_open_markets_global()
            second_cursor = adapter._global_markets_cursor
            await adapter.close()
            return first, first_cursor, second, second_cursor

        first, first_cursor, second, second_cursor = asyncio.run(exercise())

        assert cursors == [None, "next-a"]
        assert len(first) == 2
        assert len(second) == 1
        assert first_cursor == "next-a"
        assert second_cursor is None

    def test_non_sports_scan_rotates_cursor_between_refreshes(self, monkeypatch):
        monkeypatch.setenv("KALSHI_NON_SPORTS_CACHE_TTL_SECONDS", "0")
        monkeypatch.setenv("KALSHI_NON_SPORTS_PAGE_LIMIT", "2")
        monkeypatch.setenv("KALSHI_NON_SPORTS_MAX_PAGES", "1")
        monkeypatch.setenv("KALSHI_NON_SPORTS_TARGET_ODDS", "10")

        adapter = KalshiAdapter()
        cursors = []
        payloads = {
            None: {"markets": [{"ticker": "W1"}, {"ticker": "W2"}], "cursor": "next-w"},
            "next-w": {"markets": [{"ticker": "W3"}]},
        }

        async def fake_get_json(path, params=None):
            assert path == "/markets"
            cursor = (params or {}).get("cursor")
            cursors.append(cursor)
            return payloads.get(cursor, {"markets": []})

        monkeypatch.setattr(adapter, "_get_json", fake_get_json)
        monkeypatch.setattr(adapter, "_is_non_sports_market_candidate", lambda _market: True)
        monkeypatch.setattr(
            adapter,
            "_parse_market",
            lambda market: [_make_prediction_odds(f"kalshi-{market['ticker']}", "kalshi")],
        )

        async def exercise():
            first = await adapter._fetch_non_sports_markets_scan()
            first_cursor = adapter._nonsports_markets_cursor
            second = await adapter._fetch_non_sports_markets_scan()
            second_cursor = adapter._nonsports_markets_cursor
            await adapter.close()
            return first, first_cursor, second, second_cursor

        first, first_cursor, second, second_cursor = asyncio.run(exercise())

        assert cursors == [None, "next-w"]
        assert len(first) == 2
        assert len(second) == 1
        assert first_cursor == "next-w"
        assert second_cursor is None

    def test_kalshi_persists_cursors_between_instances(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PREDICTION_MARKET_STATE_DIR", str(tmp_path))
        monkeypatch.setenv("KALSHI_GLOBAL_MARKETS_PAGE_LIMIT", "2")
        monkeypatch.setenv("KALSHI_GLOBAL_MAX_PAGES", "1")
        monkeypatch.setenv("KALSHI_NON_SPORTS_CACHE_TTL_SECONDS", "0")
        monkeypatch.setenv("KALSHI_NON_SPORTS_PAGE_LIMIT", "2")
        monkeypatch.setenv("KALSHI_NON_SPORTS_MAX_PAGES", "1")
        monkeypatch.setenv("KALSHI_NON_SPORTS_TARGET_ODDS", "10")

        adapter = KalshiAdapter()

        async def fake_get_json(path, params=None):
            assert path == "/markets"
            params = params or {}
            if params.get("status") == "open":
                return {"markets": [{"ticker": "A"}, {"ticker": "B"}], "cursor": "next-global"}
            return {"markets": [{"ticker": "W1"}, {"ticker": "W2"}], "cursor": "next-nonsports"}

        monkeypatch.setattr(adapter, "_get_json", fake_get_json)
        monkeypatch.setattr(adapter, "_is_non_sports_market_candidate", lambda _market: True)
        monkeypatch.setattr(
            adapter,
            "_parse_market",
            lambda market: [_make_prediction_odds(f"kalshi-{market['ticker']}", "kalshi")],
        )

        async def exercise():
            await adapter._fetch_open_markets_global()
            await adapter._fetch_non_sports_markets_scan()
            await adapter.close()

        asyncio.run(exercise())

        reloaded = KalshiAdapter()
        try:
            snapshot = reloaded.runtime_state_snapshot()
            assert reloaded._global_markets_cursor == "next-global"
            assert reloaded._nonsports_markets_cursor == "next-nonsports"
            assert snapshot["global_markets_cursor"] == "next-global"
            assert snapshot["nonsports_markets_cursor"] == "next-nonsports"
        finally:
            asyncio.run(reloaded.close())

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


class TestPredictionMarketUnifier:
    def test_unifier_matches_similar_questions_and_unifies_event_id(self):
        poly = [
            MarketOdds(
                event_id="poly-abc",
                sport="prediction",
                market="Will Trump win the 2024 election?",
                bookmaker="polymarket",
                selection="Yes",
                odds_decimal=1.90,
                market_type="prediction",
            )
        ]
        kalshi = [
            MarketOdds(
                event_id="kalshi-xyz",
                sport="prediction",
                market="Will Donald Trump win the 2024 election?",
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=1.85,
                market_type="prediction",
            )
        ]

        unified, meta = PredictionMarketEventUnifier(min_match_score=0.5).unify(poly, kalshi)
        assert meta["matched_pairs"] == 1
        assert len(unified) == 2
        assert unified[0].event_id == unified[1].event_id
        assert unified[0].event_id.startswith("pred-")
        # Canonical market is Kalshi's market string
        assert unified[0].market == "Will Donald Trump win the 2024 election?"[:100]

    def test_unifier_does_not_match_unrelated_questions(self):
        poly = [
            MarketOdds(
                event_id="poly-1",
                sport="prediction",
                market="Will Bitcoin be above $100k by year end?",
                bookmaker="polymarket",
                selection="Yes",
                odds_decimal=2.50,
                market_type="prediction",
            )
        ]
        kalshi = [
            MarketOdds(
                event_id="kalshi-1",
                sport="prediction",
                market="Will the Lakers win tonight?",
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=1.70,
                market_type="prediction",
            )
        ]

        unified, meta = PredictionMarketEventUnifier(min_match_score=0.9).unify(poly, kalshi)
        assert meta["matched_pairs"] == 0
        assert unified[0].event_id == "poly-1"
        assert unified[1].event_id == "kalshi-1"

    def test_polymarket_yes_no_normalization(self):
        assert PolymarketAdapter._normalize_yes_no("YES") == "Yes"
        assert PolymarketAdapter._normalize_yes_no("no") == "No"
        assert PolymarketAdapter._normalize_yes_no("maybe") is None


class TestPredictionMarketDiagnostics:
    def test_weather_titles_are_categorized(self):
        counts = categorize_prediction_market_titles([
            "Will NYC snowfall exceed 3 inches this week?",
            "Will a tropical storm make landfall in Florida this month?",
        ])

        assert counts["weather"] == 2

    def test_weather_keyword_matching_avoids_hail_substring_false_positive(self):
        counts = categorize_prediction_market_titles([
            "Project Hail Mary Rotten Tomatoes score?",
            "Will Denver hail reach one inch this weekend?",
        ])

        assert counts["weather"] == 1
        assert counts["other"] == 1


class TestPredictionMarketRuntimeStore:
    def test_runtime_store_persists_recent_history(self, tmp_path):
        state_path = tmp_path / "runtime_state.json"
        store = PredictionMarketRuntimeStore(
            state_path=str(state_path),
            history_limit=2,
            payload_sample_size=1,
        )

        entry = store.record_cycle(
            market_name="prediction_markets",
            status="ok",
            source_counts={"polymarket": 2, "kalshi": 1},
            batch_count=3,
            pushed=True,
            duration_seconds=1.234,
            adapter_states={"polymarket": {"next_offset": 2}},
            matching={"matched_pairs": 1, "best_candidate_score": 0.91},
            payload=[{"event_id": "pred-1"}, {"event_id": "pred-2"}],
        )

        snapshot = store.snapshot_status()
        assert snapshot["history_size"] == 1
        assert snapshot["last_status"] == "ok"
        assert entry["payload_count"] == 2
        assert entry["payload_truncated"] is True
        assert len(entry["payload_sample"]) == 1

        reloaded = PredictionMarketRuntimeStore(
            state_path=str(state_path),
            history_limit=2,
            payload_sample_size=1,
        )
        reloaded_entry = reloaded.history(limit=1)[0]
        assert reloaded_entry["payload_sha256"] == entry["payload_sha256"]
        assert reloaded_entry["source_counts"]["polymarket"] == 2


class TestPredictionMarketPollingSource:
    def test_main_source_contains_combined_prediction_market_poll_loop(self):
        source = Path("services/market_feed/app/main.py").read_text(encoding="utf-8")

        assert "async def _prediction_market_combined_poll_loop" in source
        assert "combined_odds, meta = unifier.unify(poly_odds, kalshi_odds)" in source
        assert 'payload = [o.model_dump(mode="json") for o in combined_odds] if combined_odds else []' in source
        assert 'json=payload' in source

    def test_main_source_schedules_combined_poller_when_both_prediction_markets_enabled(self):
        source = Path("services/market_feed/app/main.py").read_text(encoding="utf-8")

        assert "if poly_enabled and kalshi_enabled:" in source
        assert "_prediction_market_combined_poll_loop(poly_adapter, kalshi_adapter)" in source
        assert '_poller_tasks["prediction_markets"] = task' in source

    def test_main_source_contains_prediction_market_status_and_history_endpoints(self):
        source = Path("services/market_feed/app/main.py").read_text(encoding="utf-8")

        assert '@app.get("/prediction-markets/status")' in source
        assert '@app.get("/prediction-markets/history")' in source
        assert "_prediction_market_runtime_store.record_cycle(" in source

    def test_weather_keyword_matching_avoids_name_substring_false_positive(self):
        counts = categorize_prediction_market_titles([
            "Mikhail Sergachev: 1+ goals",
            "Will Chicago snowfall exceed 4 inches this weekend?",
        ])

        assert counts["weather"] == 1
        assert counts["other"] == 1


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

