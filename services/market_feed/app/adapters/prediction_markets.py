"""
Prediction Market Adapters

API-based adapters for prediction markets:
- Polymarket (crypto-based, wide event coverage)
- Kalshi (CFTC-regulated, event contracts)

Cross-market arbitrage between prediction markets and sportsbooks.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from shared.schemas import MarketOdds

# Kalshi RSA-PSS imports (optional — only needed if Kalshi is enabled)
try:
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

logger = logging.getLogger(__name__)


class PolymarketAdapter:
    """
    Polymarket API adapter for prediction market odds.

    Polymarket uses CLOB (central limit order book) with prices as probabilities.
    Price of $0.65 = 65% implied probability = 1.538 decimal odds.
    """

    BASE_URL = "https://clob.polymarket.com"
    GAMMA_URL = "https://gamma-api.polymarket.com"

    # Map sportsbook events to Polymarket slugs/tags
    SPORT_TAGS = {
        "nfl": ["nfl", "football", "super-bowl"],
        "nba": ["nba", "basketball"],
        "mlb": ["mlb", "baseball", "world-series"],
        "nhl": ["nhl", "hockey", "stanley-cup"],
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.client = httpx.AsyncClient(timeout=30)

    async def fetch_markets(self, sport: Optional[str] = None) -> List[MarketOdds]:
        """Fetch all active markets, optionally filtered by sport."""
        odds_list: List[MarketOdds] = []

        try:
            # Fetch active markets from Gamma API
            response = await self.client.get(
                f"{self.GAMMA_URL}/markets",
                params={"active": "true", "closed": "false"}
            )
            response.raise_for_status()
            markets = response.json()

            for market in markets:
                # Filter by sport if specified
                if sport and not self._matches_sport(market, sport):
                    continue

                # Convert to MarketOdds
                market_odds = self._parse_market(market)
                odds_list.extend(market_odds)

        except Exception as e:
            logger.error(f"[Polymarket] Failed to fetch markets: {e}")

        logger.info(f"[Polymarket] Fetched {len(odds_list)} market odds")
        return odds_list

    def _matches_sport(self, market: Dict, sport: str) -> bool:
        """Check if market matches sport category."""
        tags = self.SPORT_TAGS.get(sport.lower(), [])
        market_tags = market.get("tags", [])
        market_question = market.get("question", "").lower()

        for tag in tags:
            if tag in [t.lower() for t in market_tags]:
                return True
            if tag in market_question:
                return True
        return False

    def _parse_market(self, market: Dict) -> List[MarketOdds]:
        """Parse Polymarket market into MarketOdds."""
        odds_list = []

        condition_id = market.get("conditionId", market.get("id", "unknown"))
        question = market.get("question", "Unknown")

        # Get outcomes with prices — Gamma API returns these as JSON strings
        raw_outcomes = market.get("outcomes", [])
        raw_prices = market.get("outcomePrices", [])
        outcomes = json.loads(raw_outcomes) if isinstance(raw_outcomes, str) else (raw_outcomes or [])
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else (raw_prices or [])

        for i, outcome in enumerate(outcomes):
            if i >= len(prices):
                break

            try:
                price = float(prices[i])
                if price <= 0 or price >= 1:
                    continue

                # Convert probability to decimal odds
                decimal_odds = round(1 / price, 4)

                odds_list.append(MarketOdds(
                    event_id=f"poly-{condition_id}",
                    sport="prediction",
                    market=question[:100],
                    bookmaker="polymarket",
                    selection=outcome,
                    odds_decimal=decimal_odds,
                    market_type="prediction",
                    expires_at=self._parse_end_date(market),
                ))
            except (ValueError, TypeError) as e:
                logger.warning(f"Failed to parse outcome {outcome}: {e}")

        return odds_list

    def _parse_end_date(self, market: Dict) -> Optional[datetime]:
        """Parse market end date."""
        end_str = market.get("endDateIso") or market.get("endDate")
        if end_str:
            try:
                return datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class KalshiAdapter:
    """
    Kalshi API adapter for CFTC-regulated event contracts.

    Kalshi prices are in cents (0-100), representing probability.
    Price of 65 cents = 65% probability = 1.538 decimal odds.
    """

    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
    DEMO_URL = "https://demo-api.kalshi.co/trade-api/v2"

    # Kalshi event categories that overlap with sports
    SPORTS_CATEGORIES = ["sports", "nfl", "nba", "mlb", "nhl"]

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.use_demo = self.config.get("use_demo", False)
        self.base_url = self.DEMO_URL if self.use_demo else self.BASE_URL
        self.client = httpx.AsyncClient(timeout=30)

        # Load API key and private key for RSA-PSS authentication
        self.api_key = os.getenv("KALSHI_API_KEY")
        private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")

        self.private_key = None
        if private_key_path and os.path.exists(private_key_path):
            if not _HAS_CRYPTO:
                logger.error("[Kalshi] cryptography package not installed — cannot authenticate")
            else:
                try:
                    with open(private_key_path, "rb") as key_file:
                        self.private_key = serialization.load_pem_private_key(
                            key_file.read(),
                            password=None,
                            backend=default_backend(),
                        )
                    logger.info("[Kalshi] RSA private key loaded successfully")
                except Exception as e:
                    logger.error(f"[Kalshi] Failed to load private key: {e}")
        else:
            if not private_key_path:
                logger.warning("[Kalshi] KALSHI_PRIVATE_KEY_PATH not set")
            elif not os.path.exists(private_key_path):
                logger.warning(f"[Kalshi] Key file not found: {private_key_path}")

    # ── RSA-PSS Signing ────────────────────────────────────────────────────

    def _sign_request(self, method: str, path: str) -> tuple:
        """
        Create RSA-PSS signature for a Kalshi API request.

        Kalshi auth headers:
          KALSHI-ACCESS-KEY       – API key id
          KALSHI-ACCESS-TIMESTAMP – current epoch ms
          KALSHI-ACCESS-SIGNATURE – base64(RSA-PSS(timestamp + METHOD + path_no_query))

        Returns:
            (signature_b64, timestamp_str)
        """
        if not self.private_key:
            raise RuntimeError("Kalshi private key not loaded — cannot sign request")

        timestamp = str(int(datetime.utcnow().timestamp() * 1000))
        path_without_query = path.split("?")[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")

        signature = self.private_key.sign(
            message,
            asym_padding.PSS(
                mgf=asym_padding.MGF1(hashes.SHA256()),
                salt_length=asym_padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8"), timestamp

    def _auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """Return Kalshi auth headers dict (or empty if keys missing)."""
        if not self.api_key or not self.private_key:
            return {}
        try:
            sig, ts = self._sign_request(method, path)
            return {
                "KALSHI-ACCESS-KEY": self.api_key,
                "KALSHI-ACCESS-SIGNATURE": sig,
                "KALSHI-ACCESS-TIMESTAMP": ts,
            }
        except Exception as e:
            logger.error(f"[Kalshi] Failed to sign request: {e}")
            return {}

    # ── Market Fetching ────────────────────────────────────────────────────

    # All sport series tickers we care about
    SPORT_SERIES = {
        "basketball_nba": "KXNBA",
        "americanfootball_nfl": "KXNFL",
        "baseball_mlb": "KXMLB",
        "icehockey_nhl": "KXNHL",
    }

    async def fetch_markets(self, sport: Optional[str] = None) -> List[MarketOdds]:
        """Fetch open markets from Kalshi (public endpoint, no auth needed).

        When sport is None, fetches ALL sport series (NBA, NFL, MLB, NHL).
        The default /markets endpoint returns mostly MVE parlay markets with
        zero prices, so we must query by series_ticker explicitly.
        """
        odds_list: List[MarketOdds] = []

        # Determine which series to fetch
        if sport and sport.lower() in self.SPORT_SERIES:
            series_to_fetch = {sport.lower(): self.SPORT_SERIES[sport.lower()]}
        else:
            # Fetch ALL sport series
            series_to_fetch = dict(self.SPORT_SERIES)

        for sport_key, series_ticker in series_to_fetch.items():
            try:
                params: Dict[str, Any] = {
                    "status": "open",
                    "limit": 200,
                    "series_ticker": series_ticker,
                }

                response = await self.client.get(
                    f"{self.base_url}/markets",
                    params=params,
                )
                response.raise_for_status()
                data = response.json()
                markets = data.get("markets", [])

                for market in markets:
                    # Skip multivariate (parlay) markets — too complex for arb
                    if market.get("ticker", "").startswith("KXMVE"):
                        continue

                    market_odds = self._parse_market(market)
                    odds_list.extend(market_odds)

                logger.debug(f"[Kalshi] {series_ticker}: {len(markets)} markets")

            except Exception as e:
                logger.error(f"[Kalshi] Failed to fetch {series_ticker}: {e}")

        logger.info(f"[Kalshi] Fetched {len(odds_list)} market odds")
        return odds_list

    def _parse_market(self, market: Dict) -> List[MarketOdds]:
        """Parse Kalshi market into MarketOdds."""
        odds_list = []

        ticker = market.get("ticker", "unknown")
        title = market.get("title", market.get("subtitle", "Unknown"))

        # Get best bid/ask prices (in cents)
        yes_bid = market.get("yes_bid", 0)  # Best bid for YES
        yes_ask = market.get("yes_ask", 100)  # Best ask for YES
        no_bid = market.get("no_bid", 0)  # Best bid for NO
        no_ask = market.get("no_ask", 100)  # Best ask for NO

        # Use midpoint for fair value
        yes_price = (yes_bid + yes_ask) / 200  # Convert cents to probability
        no_price = (no_bid + no_ask) / 200

        # Calculate decimal odds
        if 0 < yes_price < 1:
            odds_list.append(MarketOdds(
                event_id=f"kalshi-{ticker}",
                sport="prediction",
                market=title[:100],
                bookmaker="kalshi",
                selection="Yes",
                odds_decimal=round(1 / yes_price, 4),
                market_type="prediction",
                expires_at=self._parse_expiration(market),
            ))

        if 0 < no_price < 1:
            odds_list.append(MarketOdds(
                event_id=f"kalshi-{ticker}",
                sport="prediction",
                market=title[:100],
                bookmaker="kalshi",
                selection="No",
                odds_decimal=round(1 / no_price, 4),
                market_type="prediction",
                expires_at=self._parse_expiration(market),
            ))

        return odds_list

    def _parse_expiration(self, market: Dict) -> Optional[datetime]:
        """Parse market expiration date."""
        exp_str = market.get("expiration_time")
        if exp_str:
            try:
                return datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
            except ValueError:
                pass
        return None

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()


class PredictionMarketArbFinder:
    """
    Finds arbitrage opportunities between prediction markets and sportsbooks.

    Maps similar events across platforms and detects pricing discrepancies.
    """

    def __init__(self):
        self.polymarket = PolymarketAdapter()
        self.kalshi = KalshiAdapter()

    async def find_cross_market_arbs(
        self,
        sportsbook_odds: List[MarketOdds],
        sport: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Find arbitrage opportunities between prediction markets and sportsbooks.
        """
        arbs = []

        # Fetch prediction market odds
        poly_odds = await self.polymarket.fetch_markets(sport)
        kalshi_odds = await self.kalshi.fetch_markets(sport)

        # Combine all odds
        prediction_odds = poly_odds + kalshi_odds

        # Try to match events and find arbs
        for sb_odds in sportsbook_odds:
            matches = self._find_matching_prediction(sb_odds, prediction_odds)
            for pred_odds in matches:
                arb = self._check_arbitrage(sb_odds, pred_odds)
                if arb:
                    arbs.append(arb)

        return arbs

    def _find_matching_prediction(
        self,
        sb_odds: MarketOdds,
        prediction_odds: List[MarketOdds]
    ) -> List[MarketOdds]:
        """Find prediction market odds that match a sportsbook event."""
        matches = []

        # Extract key terms from sportsbook odds
        sb_terms = self._extract_terms(sb_odds.market + " " + sb_odds.selection)

        for pred in prediction_odds:
            pred_terms = self._extract_terms(pred.market + " " + pred.selection)

            # Check for significant term overlap
            overlap = len(sb_terms & pred_terms)
            if overlap >= 2:  # At least 2 common terms
                matches.append(pred)

        return matches

    def _extract_terms(self, text: str) -> set:
        """Extract searchable terms from text."""
        # Remove common words and extract key terms
        text = text.lower()
        words = re.findall(r'\b[a-z]+\b', text)
        stopwords = {"the", "a", "an", "to", "win", "will", "be", "is", "vs", "at"}
        return set(w for w in words if w not in stopwords and len(w) > 2)

    def _check_arbitrage(
        self,
        sb_odds: MarketOdds,
        pred_odds: MarketOdds
    ) -> Optional[Dict[str, Any]]:
        """Check if two odds create an arbitrage opportunity."""
        # Calculate implied probabilities
        sb_prob = 1 / sb_odds.odds_decimal
        pred_prob = 1 / pred_odds.odds_decimal

        # Check for arb (probabilities sum to < 1)
        total_prob = sb_prob + (1 - pred_prob)  # Opposing sides

        if total_prob < 0.98:  # At least 2% edge
            edge = (1 - total_prob) * 100
            return {
                "type": "cross_market",
                "edge_percentage": round(edge, 2),
                "leg1": {
                    "bookmaker": sb_odds.bookmaker,
                    "market": sb_odds.market,
                    "selection": sb_odds.selection,
                    "odds": sb_odds.odds_decimal,
                },
                "leg2": {
                    "bookmaker": pred_odds.bookmaker,
                    "market": pred_odds.market,
                    "selection": pred_odds.selection,
                    "odds": pred_odds.odds_decimal,
                },
            }

        return None

    async def close(self):
        """Close all adapters."""
        await self.polymarket.close()
        await self.kalshi.close()

