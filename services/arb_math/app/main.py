from __future__ import annotations

import os
import random
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI

from shared.schemas import (
    ArbOpportunity,
    ArbRequest,
    ArbResponse,
    HealthResponse,
    MarketOdds,
    PromoConvertRequest,
    PromoConvertResponse,
)

SERVICE_NAME = os.getenv("SERVICE_NAME", "arb_math")

# Tiered alert thresholds (profit percentage)
TIER_FIRE = 3.0       # 🔥 >3% profit
TIER_LIGHTNING = 1.5  # ⚡ 1.5-3% profit
# Below 1.5% = ℹ️ info tier

# Stake configuration (anti-detection)
MAX_TOTAL_STAKE = float(os.getenv("MAX_TOTAL_STAKE", "1000.0"))
MIN_TOTAL_STAKE = float(os.getenv("MIN_TOTAL_STAKE", "100.0"))
RANDOMIZE_STAKES = os.getenv("RANDOMIZE_STAKES", "true").lower() == "true"
STAKE_RANDOMIZATION_PCT = float(os.getenv("STAKE_RANDOMIZATION_PCT", "0.15"))

# Minimum arb profit threshold — filters out noise / same-book rounding arbs
MIN_ARB_PROFIT_PCT = float(os.getenv("MIN_ARB_PROFIT_PCT", "0.5"))

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

    Applies randomization if RANDOMIZE_STAKES is enabled to prevent detection.

    Returns list of leg dictionaries with:
    - bookmaker, selection, odds, stake, payout
    """
    # Apply randomization to total_stake BEFORE calculating legs
    if RANDOMIZE_STAKES:
        variance = random.uniform(-STAKE_RANDOMIZATION_PCT, STAKE_RANDOMIZATION_PCT)
        total_stake = total_stake * (1 + variance)
        # Clamp to min/max bounds
        total_stake = max(MIN_TOTAL_STAKE, min(MAX_TOTAL_STAKE, total_stake))

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


# ─────────────────────────────────────────────────────────────────────────────
# +EV Detection Functions
# ─────────────────────────────────────────────────────────────────────────────

MIN_EV_THRESHOLD = float(os.getenv("MIN_EV_THRESHOLD", "2.0"))  # 2% minimum edge


def _calculate_no_vig_probabilities(odds_by_selection: Dict[str, float]) -> Dict[str, float]:
    """
    Remove vig from odds to get true probabilities.

    For a 2-way market: implied_sum = 1/odds_a + 1/odds_b (includes vig)
    True probability = implied_prob / implied_sum (removes vig proportionally)
    """
    implied_probs = {sel: 1.0 / odds for sel, odds in odds_by_selection.items()}
    implied_sum = sum(implied_probs.values())

    if implied_sum == 0:
        return {}

    return {sel: prob / implied_sum for sel, prob in implied_probs.items()}


def _detect_positive_ev(
    odds_list: List[MarketOdds],
    min_ev_pct: float = MIN_EV_THRESHOLD,
) -> List[ArbOpportunity]:
    """
    Detect +EV opportunities by comparing offered odds to no-vig fair odds.

    Algorithm:
    1. Group by event+market
    2. For each group, get best odds per selection across all books
    3. Calculate no-vig "true" probabilities from best available odds
    4. Check each individual book's odds against fair odds
    5. If EV% > threshold, flag as +EV opportunity
    """
    from collections import defaultdict

    grouped: Dict[Tuple[str, str], List[MarketOdds]] = defaultdict(list)
    for entry in odds_list:
        grouped[(entry.event_id, entry.market)].append(entry)

    ev_opportunities: List[ArbOpportunity] = []

    for (event_id, market), group in grouped.items():
        # Get best odds for each selection (used to derive true probability)
        best_odds: Dict[str, float] = {}
        for entry in group:
            if entry.selection not in best_odds or entry.odds_decimal > best_odds[entry.selection]:
                best_odds[entry.selection] = entry.odds_decimal

        if len(best_odds) < 2:
            continue  # Need at least 2 sides for no-vig calculation

        # Calculate true probabilities from best available odds
        true_probs = _calculate_no_vig_probabilities(best_odds)

        # Check each individual offering for +EV
        for entry in group:
            if entry.selection not in true_probs:
                continue

            true_prob = true_probs[entry.selection]
            fair_odds = 1.0 / true_prob if true_prob > 0 else float('inf')

            # EV% = (offered_odds / fair_odds - 1) * 100
            ev_pct = (entry.odds_decimal / fair_odds - 1) * 100

            if ev_pct >= min_ev_pct:
                # Kelly fraction = (p * odds - 1) / (odds - 1) where p = true_prob
                kelly = (true_prob * entry.odds_decimal - 1) / (entry.odds_decimal - 1) if entry.odds_decimal > 1 else 0
                kelly = max(0, min(kelly, 0.25))  # Cap at 25% of bankroll

                ev_opportunities.append(ArbOpportunity(
                    event_id=event_id,
                    market=market,
                    implied_prob_sum=sum(1/o for o in best_odds.values()),
                    has_arb=False,
                    opportunity_type="positive_ev",
                    ev_percentage=round(ev_pct, 2),
                    true_probability=round(true_prob, 4),
                    kelly_fraction=round(kelly, 4),
                    notes=f"📈 +{ev_pct:.1f}% EV on {entry.selection} @ {entry.bookmaker} ({entry.odds_decimal})",
                    legs=[{
                        "bookmaker": entry.bookmaker,
                        "selection": entry.selection,
                        "odds_decimal": entry.odds_decimal,
                        "fair_odds": round(fair_odds, 3),
                        "ev_percentage": round(ev_pct, 2),
                    }],
                ))

    return ev_opportunities


# ─────────────────────────────────────────────────────────────────────────────
# Middles Detection Functions
# ─────────────────────────────────────────────────────────────────────────────

def _detect_middles(odds_list: List[MarketOdds]) -> List[ArbOpportunity]:
    """
    Detect middle opportunities where both sides of a spread/total can win.

    Examples:
    - Spread: DK Lakers -3.5 + FD Celtics +5.5 → middle at 4-5 points
    - Total: DK Over 210.5 + FD Under 213.5 → middle at 211-213
    """
    from collections import defaultdict

    # Group by event + market_type (spread or total)
    grouped: Dict[Tuple[str, str], List[MarketOdds]] = defaultdict(list)
    for entry in odds_list:
        if entry.market_type in ("spread", "total") and entry.line is not None:
            grouped[(entry.event_id, entry.market_type)].append(entry)

    middle_opportunities: List[ArbOpportunity] = []

    for (event_id, market_type), group in grouped.items():
        # Separate by bookmaker
        by_book: Dict[str, List[MarketOdds]] = defaultdict(list)
        for entry in group:
            by_book[entry.bookmaker].append(entry)

        bookmakers = list(by_book.keys())
        if len(bookmakers) < 2:
            continue

        # Check each pair of bookmakers for middles
        for i, book1 in enumerate(bookmakers):
            for book2 in bookmakers[i+1:]:
                _check_middle_pair(
                    by_book[book1], by_book[book2],
                    event_id, market_type, middle_opportunities
                )

    return middle_opportunities


def _check_middle_pair(
    book1_odds: List[MarketOdds],
    book2_odds: List[MarketOdds],
    event_id: str,
    market_type: str,
    results: List[ArbOpportunity],
) -> None:
    """Check a pair of bookmakers for middle opportunities."""
    for o1 in book1_odds:
        for o2 in book2_odds:
            if o1.line is None or o2.line is None:
                continue

            middle_gap = None
            middle_range = None

            if market_type == "spread":
                # For spreads: middle exists if lines don't overlap
                # e.g., Team A -3.5 (line=-3.5) vs Team B +5.5 (line=+5.5)
                # Middle if abs(line1) < abs(line2) and opposite signs
                if o1.line < 0 and o2.line > 0:  # o1 is favorite spread, o2 is underdog spread
                    if abs(o1.line) < o2.line:
                        middle_gap = o2.line - abs(o1.line)
                        low = int(abs(o1.line)) + 1
                        high = int(o2.line)
                        middle_range = f"{low}-{high} points"
                elif o1.line > 0 and o2.line < 0:  # opposite
                    if o1.line > abs(o2.line):
                        middle_gap = o1.line - abs(o2.line)
                        low = int(abs(o2.line)) + 1
                        high = int(o1.line)
                        middle_range = f"{low}-{high} points"

            elif market_type == "total":
                # For totals: middle exists if over line < under line
                is_over_1 = "over" in o1.selection.lower()
                is_under_2 = "under" in o2.selection.lower()

                if is_over_1 and is_under_2 and o1.line < o2.line:
                    middle_gap = o2.line - o1.line
                    middle_range = f"{o1.line + 0.5:.1f}-{o2.line - 0.5:.1f}"
                elif "under" in o1.selection.lower() and "over" in o2.selection.lower() and o2.line < o1.line:
                    middle_gap = o1.line - o2.line
                    middle_range = f"{o2.line + 0.5:.1f}-{o1.line - 0.5:.1f}"

            if middle_gap and middle_gap > 0:
                # Estimate probability of hitting middle (rough approximation)
                middle_prob = min(0.15, middle_gap * 0.03)  # ~3% per point of gap

                results.append(ArbOpportunity(
                    event_id=event_id,
                    market=f"{market_type}_middle",
                    implied_prob_sum=1.0,
                    has_arb=False,
                    opportunity_type="middle",
                    middle_range=middle_range,
                    middle_gap=round(middle_gap, 1),
                    middle_probability=round(middle_prob, 3),
                    notes=f"🎯 Middle: {middle_range} ({middle_gap:.1f}pt gap, ~{middle_prob*100:.0f}% hit rate)",
                    legs=[
                        {"bookmaker": o1.bookmaker, "selection": o1.selection, "odds_decimal": o1.odds_decimal, "line": o1.line},
                        {"bookmaker": o2.bookmaker, "selection": o2.selection, "odds_decimal": o2.odds_decimal, "line": o2.line},
                    ],
                ))


@app.post("/arbitrage", response_model=ArbResponse)
def evaluate_arbitrage(
    payload: ArbRequest,
    min_profit_pct: Optional[float] = None,
    total_stake: float = MAX_TOTAL_STAKE,  # Use configured max stake
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

    # Use explicit threshold or fall back to configured minimum
    effective_min = min_profit_pct if min_profit_pct is not None else MIN_ARB_PROFIT_PCT

    opportunities: List[ArbOpportunity] = []
    for (event_id, market), group in grouped.items():
        best_by_selection = _best_odds_by_selection(group)

        implied_sum = sum(
            1.0 / odds for odds, _ in best_by_selection.values()
        ) if best_by_selection else 1.0

        has_arb = implied_sum < 1.0
        profit_pct = _calculate_profit_percentage(implied_sum)

        # Skip if below minimum profit threshold
        if profit_pct < effective_min:
            continue

        # Skip same-bookmaker "arbs" — not real arbitrage
        bookmakers_in_arb = {mo.bookmaker for _, mo in best_by_selection.values()}
        if len(bookmakers_in_arb) < 2:
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


# ─────────────────────────────────────────────────────────────────────────────
# +EV Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/positive-ev", response_model=ArbResponse)
def evaluate_positive_ev(
    payload: ArbRequest,
    min_ev_pct: float = MIN_EV_THRESHOLD,
) -> ArbResponse:
    """
    Detect +EV (Positive Expected Value) opportunities.

    Compares offered odds to fair (no-vig) odds derived from the best
    available lines across bookmakers.
    """
    ev_opps = _detect_positive_ev(payload.odds, min_ev_pct)
    return ArbResponse(opportunities=ev_opps, evaluated_at=datetime.utcnow())


# ─────────────────────────────────────────────────────────────────────────────
# Middles Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/middles", response_model=ArbResponse)
def evaluate_middles(payload: ArbRequest) -> ArbResponse:
    """
    Detect middle opportunities where both sides of a spread/total can win.

    Requires odds with market_type='spread' or 'total' and line values.
    """
    middle_opps = _detect_middles(payload.odds)
    return ArbResponse(opportunities=middle_opps, evaluated_at=datetime.utcnow())


# ─────────────────────────────────────────────────────────────────────────────
# Promo Converter Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/promo-convert", response_model=PromoConvertResponse)
def convert_promo(request: PromoConvertRequest) -> PromoConvertResponse:
    """
    Calculate optimal hedge for converting free bets or profit boosts to cash.

    Free Bet (doesn't return stake):
        hedge_stake = amount * (promo_odds - 1) / hedge_odds
        guaranteed_profit = hedge_stake * (hedge_odds - 1)

    Free Bet (returns stake):
        hedge_stake = amount * promo_odds / hedge_odds
        guaranteed_profit = hedge_stake * (hedge_odds - 1)

    Profit Boost:
        effective_odds = 1 + (promo_odds - 1) * (1 + boost_pct/100)
        hedge_stake = amount * effective_odds / hedge_odds
        guaranteed_profit = hedge_stake * (hedge_odds - 1) - amount
    """
    promo_odds = request.odds_decimal
    hedge_odds = request.hedge_odds_decimal
    amount = request.amount

    if request.promo_type == "free_bet":
        if request.free_bet_returns_stake:
            # Free bet returns stake on win
            promo_payout = amount * promo_odds
            hedge_stake = (amount * promo_odds) / hedge_odds
        else:
            # Free bet does NOT return stake (most common)
            promo_payout = amount * (promo_odds - 1)
            hedge_stake = (amount * (promo_odds - 1)) / hedge_odds

        hedge_payout = hedge_stake * hedge_odds
        guaranteed_profit = hedge_stake * (hedge_odds - 1)
        conversion_rate = guaranteed_profit / amount

        notes = f"Free bet ${amount:.2f} → ${guaranteed_profit:.2f} guaranteed ({conversion_rate*100:.1f}% conversion)"

    elif request.promo_type == "profit_boost":
        boost_pct = request.boost_percentage or 0
        effective_odds = 1 + (promo_odds - 1) * (1 + boost_pct / 100)
        promo_payout = amount * effective_odds

        hedge_stake = (amount * effective_odds) / hedge_odds
        hedge_payout = hedge_stake * hedge_odds

        # Profit = hedge_payout - hedge_stake - amount (you risk the promo stake)
        guaranteed_profit = hedge_stake * (hedge_odds - 1) - amount
        conversion_rate = guaranteed_profit / amount if amount > 0 else 0

        notes = f"{boost_pct:.0f}% boost: effective odds {effective_odds:.3f}, profit ${guaranteed_profit:.2f}"

    else:
        # Unknown promo type - return zeros
        return PromoConvertResponse(
            promo_type=request.promo_type,
            promo_amount=amount,
            promo_side_odds=promo_odds,
            hedge_side_odds=hedge_odds,
            recommended_hedge_stake=0,
            guaranteed_profit=0,
            conversion_rate=0,
            promo_side_payout=0,
            hedge_side_payout=0,
            notes=f"Unknown promo type: {request.promo_type}",
        )

    return PromoConvertResponse(
        promo_type=request.promo_type,
        promo_amount=amount,
        promo_side_odds=promo_odds,
        hedge_side_odds=hedge_odds,
        recommended_hedge_stake=round(hedge_stake, 2),
        guaranteed_profit=round(guaranteed_profit, 2),
        conversion_rate=round(conversion_rate, 4),
        promo_side_payout=round(promo_payout, 2),
        hedge_side_payout=round(hedge_payout, 2),
        notes=notes,
    )