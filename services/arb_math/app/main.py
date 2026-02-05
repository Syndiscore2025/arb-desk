from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI

from shared.schemas import ArbOpportunity, ArbRequest, ArbResponse, HealthResponse, MarketOdds

SERVICE_NAME = os.getenv("SERVICE_NAME", "arb_math")

# Tiered alert thresholds (profit percentage)
TIER_FIRE = 3.0       # 🔥 >3% profit
TIER_LIGHTNING = 1.5  # ⚡ 1.5-3% profit
# Below 1.5% = ℹ️ info tier

app = FastAPI(title="Arbitrage Math", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


def _best_odds_by_selection(
    odds: List[MarketOdds]
) -> Dict[str, Tuple[float, MarketOdds]]:
    """Get best odds for each selection with the full MarketOdds object."""
    best: Dict[str, Tuple[float, MarketOdds]] = {}
    for entry in odds:
        current = best.get(entry.selection)
        if current is None or entry.odds_decimal > current[0]:
            best[entry.selection] = (entry.odds_decimal, entry)
    return best


def _group_key(entry: MarketOdds) -> Tuple[str, str]:
    return (entry.event_id, entry.market)


def _calculate_profit_percentage(implied_sum: float) -> float:
    """Calculate profit percentage from implied probability sum."""
    if implied_sum >= 1.0:
        return 0.0
    return ((1.0 / implied_sum) - 1.0) * 100


def _calculate_stakes(
    best_by_selection: Dict[str, Tuple[float, MarketOdds]],
    total_stake: float = 1000.0,
) -> List[Dict]:
    """
    Calculate optimal stakes for each leg to guarantee profit.

    Returns list of leg dictionaries with:
    - bookmaker, selection, odds, stake, payout
    """
    implied_sum = sum(1.0 / odds for odds, _ in best_by_selection.values())

    legs = []
    for selection, (odds, market_odds) in best_by_selection.items():
        # Stake = (Total / implied_sum) / odds
        stake = (total_stake / implied_sum) / odds
        payout = stake * odds

        legs.append({
            "bookmaker": market_odds.bookmaker,
            "selection": selection,
            "odds_decimal": odds,
            "stake": round(stake, 2),
            "payout": round(payout, 2),
            "sport": market_odds.sport,
            "market": market_odds.market,
            "event_id": market_odds.event_id,
        })

    return legs


def _get_tier(profit_pct: float) -> str:
    """Get alert tier based on profit percentage."""
    if profit_pct >= TIER_FIRE:
        return "fire"
    elif profit_pct >= TIER_LIGHTNING:
        return "lightning"
    else:
        return "info"


@app.post("/arbitrage", response_model=ArbResponse)
def evaluate_arbitrage(
    payload: ArbRequest,
    min_profit_pct: Optional[float] = None,
    total_stake: float = 1000.0,
) -> ArbResponse:
    """
    Evaluate arbitrage opportunities with enhanced details.

    Args:
        payload: ArbRequest with list of odds
        min_profit_pct: Optional minimum profit percentage filter
        total_stake: Total stake for calculating individual leg amounts
    """
    grouped: Dict[Tuple[str, str], List[MarketOdds]] = {}
    for entry in payload.odds:
        grouped.setdefault(_group_key(entry), []).append(entry)

    opportunities: List[ArbOpportunity] = []
    for (event_id, market), group in grouped.items():
        best_by_selection = _best_odds_by_selection(group)

        implied_sum = sum(
            1.0 / odds for odds, _ in best_by_selection.values()
        ) if best_by_selection else 1.0

        has_arb = implied_sum < 1.0
        profit_pct = _calculate_profit_percentage(implied_sum)

        # Skip if below minimum profit threshold
        if min_profit_pct and profit_pct < min_profit_pct:
            continue

        # Build leg details for arbs
        legs = []
        if has_arb:
            legs = _calculate_stakes(best_by_selection, total_stake)

        tier = _get_tier(profit_pct) if has_arb else "info"

        note = None
        if has_arb:
            note = f"🎯 {profit_pct:.2f}% arb ({tier}). Stakes for ${total_stake:.0f}."

        opportunities.append(
            ArbOpportunity(
                event_id=event_id,
                market=market,
                implied_prob_sum=round(implied_sum, 6),
                has_arb=has_arb,
                notes=note,
                profit_percentage=round(profit_pct, 2) if has_arb else None,
                legs=legs,
                is_live=False,  # Will be set by caller if from live feed
            )
        )

    return ArbResponse(opportunities=opportunities, evaluated_at=datetime.utcnow())