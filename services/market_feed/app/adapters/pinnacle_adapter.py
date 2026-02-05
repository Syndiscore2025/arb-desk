"""
Pinnacle Adapter

API-based adapter for Pinnacle (sharp offshore book).

Pinnacle is considered the "sharpest" book with the most efficient lines.
Used for:
- Closing Line Value (CLV) comparison
- Fair odds reference
- Sharp line hedging

Note: Requires VPN/proxy for US access. API requires auth.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from shared.schemas import MarketOdds

logger = logging.getLogger(__name__)


# Sport IDs for Pinnacle API
PINNACLE_SPORTS = {
    "nba": 4,
    "nfl": 1,
    "mlb": 3,
    "nhl": 19,
    "ncaaf": 15,
    "ncaab": 5,
    "soccer": 29,
}


class PinnacleAdapter:
    """
    Pinnacle API adapter for sharp line comparison.

    Pinnacle lines are considered the most efficient in the market.
    Use for CLV (Closing Line Value) and fair odds reference.
    """

    BASE_URL = "https://api.pinnacle.com/v1"

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.api_key = config.get("pinnacle_api_key", "")
        self.proxy = config.get("proxy")  # VPN/proxy for US access

        # Setup client with auth
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Basic {self.api_key}"

        proxies = {"all://": self.proxy} if self.proxy else None
        self.client = httpx.AsyncClient(
            timeout=30,
            headers=headers,
            proxies=proxies,
        )

    async def fetch_odds(self, sport: str) -> List[MarketOdds]:
        """Fetch odds for a sport from Pinnacle."""
        odds_list: List[MarketOdds] = []

        sport_id = PINNACLE_SPORTS.get(sport.lower())
        if not sport_id:
            logger.warning(f"[Pinnacle] Unknown sport: {sport}")
            return odds_list

        try:
            # Fetch fixtures first
            fixtures = await self._fetch_fixtures(sport_id)
            if not fixtures:
                return odds_list

            # Fetch odds for fixtures
            response = await self.client.get(
                f"{self.BASE_URL}/odds",
                params={"sportId": sport_id, "oddsFormat": "Decimal"}
            )
            response.raise_for_status()
            data = response.json()

            leagues = data.get("leagues", [])
            for league in leagues:
                for event in league.get("events", []):
                    event_id = event.get("id")
                    fixture = fixtures.get(event_id, {})

                    event_odds = self._parse_event_odds(event, fixture, sport)
                    odds_list.extend(event_odds)

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.error("[Pinnacle] Authentication failed - check API key")
            elif e.response.status_code == 403:
                logger.error("[Pinnacle] Access denied - may need VPN/proxy")
            else:
                logger.error(f"[Pinnacle] HTTP error: {e}")
        except Exception as e:
            logger.error(f"[Pinnacle] Failed to fetch odds: {e}")

        logger.info(f"[Pinnacle] Fetched {len(odds_list)} odds for {sport}")
        return odds_list

    async def _fetch_fixtures(self, sport_id: int) -> Dict[int, Dict]:
        """Fetch fixtures (events) for a sport."""
        fixtures = {}

        try:
            response = await self.client.get(
                f"{self.BASE_URL}/fixtures",
                params={"sportId": sport_id}
            )
            response.raise_for_status()
            data = response.json()

            for league in data.get("leagues", []):
                for event in league.get("events", []):
                    fixtures[event.get("id")] = {
                        "home": event.get("home"),
                        "away": event.get("away"),
                        "starts": event.get("starts"),
                        "league": league.get("name"),
                    }

        except Exception as e:
            logger.warning(f"[Pinnacle] Failed to fetch fixtures: {e}")

        return fixtures

    def _parse_event_odds(
        self,
        event: Dict,
        fixture: Dict,
        sport: str
    ) -> List[MarketOdds]:
        """Parse event odds into MarketOdds."""
        odds_list = []

        event_id = f"pinnacle-{event.get('id')}"
        home = fixture.get("home", "Home")
        away = fixture.get("away", "Away")
        starts = fixture.get("starts")

        # Parse start time
        expires_at = None
        if starts:
            try:
                expires_at = datetime.fromisoformat(starts.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Parse periods (full game, 1st half, etc.)
        for period in event.get("periods", []):
            period_num = period.get("number", 0)
            period_label = "full_game" if period_num == 0 else f"period_{period_num}"

            # Moneyline odds
            ml = period.get("moneyline")
            if ml:
                if ml.get("home"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=home,
                        odds_decimal=ml["home"],
                        market_type="moneyline",
                        period=period_label,
                        expires_at=expires_at,
                    ))
                if ml.get("away"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=away,
                        odds_decimal=ml["away"],
                        market_type="moneyline",
                        period=period_label,
                        expires_at=expires_at,
                    ))

            # Spread odds
            spreads = period.get("spreads", [])
            for spread in spreads:
                hdp = spread.get("hdp", 0)  # Handicap
                if spread.get("home"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=f"{home} {hdp:+.1f}",
                        odds_decimal=spread["home"],
                        market_type="spread",
                        line=hdp,
                        period=period_label,
                        expires_at=expires_at,
                    ))
                if spread.get("away"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=f"{away} {-hdp:+.1f}",
                        odds_decimal=spread["away"],
                        market_type="spread",
                        line=-hdp,
                        period=period_label,
                        expires_at=expires_at,
                    ))

            # Total odds
            totals = period.get("totals", [])
            for total in totals:
                points = total.get("points", 0)
                if total.get("over"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=f"Over {points}",
                        odds_decimal=total["over"],
                        market_type="total",
                        line=points,
                        period=period_label,
                        expires_at=expires_at,
                    ))
                if total.get("under"):
                    odds_list.append(MarketOdds(
                        event_id=event_id,
                        sport=sport,
                        market=f"{home} vs {away}",
                        bookmaker="pinnacle",
                        selection=f"Under {points}",
                        odds_decimal=total["under"],
                        market_type="total",
                        line=points,
                        period=period_label,
                        expires_at=expires_at,
                    ))

        return odds_list

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class CLVCalculator:
    """
    Closing Line Value (CLV) calculator.

    Compares your bet odds to closing odds (Pinnacle) to measure edge.
    Positive CLV = you beat the market.
    """

    def __init__(self, pinnacle: PinnacleAdapter):
        self.pinnacle = pinnacle
        self.bet_history: List[Dict] = []

    def record_bet(
        self,
        event_id: str,
        selection: str,
        odds_at_bet: float,
        stake: float,
        bookmaker: str,
    ):
        """Record a bet for CLV tracking."""
        self.bet_history.append({
            "event_id": event_id,
            "selection": selection,
            "odds_at_bet": odds_at_bet,
            "stake": stake,
            "bookmaker": bookmaker,
            "bet_time": datetime.utcnow(),
            "closing_odds": None,
            "clv": None,
        })

    async def calculate_clv(self, sport: str) -> List[Dict]:
        """
        Calculate CLV for all recorded bets.

        CLV% = (Your Odds / Closing Odds - 1) * 100
        """
        # Fetch current Pinnacle odds as "closing" reference
        pinnacle_odds = await self.pinnacle.fetch_odds(sport)
        odds_map = {
            (o.event_id, o.selection): o.odds_decimal
            for o in pinnacle_odds
        }

        results = []
        for bet in self.bet_history:
            # Try to find matching closing odds
            closing = odds_map.get((bet["event_id"], bet["selection"]))

            if closing:
                clv = ((bet["odds_at_bet"] / closing) - 1) * 100
                bet["closing_odds"] = closing
                bet["clv"] = round(clv, 2)
                results.append(bet)

        return results
