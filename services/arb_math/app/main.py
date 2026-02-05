from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, List, Tuple

from fastapi import FastAPI

from shared.schemas import ArbOpportunity, ArbRequest, ArbResponse, HealthResponse, MarketOdds

SERVICE_NAME = os.getenv("SERVICE_NAME", "arb_math")

app = FastAPI(title="Arbitrage Math", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


def _best_odds_by_selection(odds: List[MarketOdds]) -> Dict[str, float]:
    best: Dict[str, float] = {}
    for entry in odds:
        current = best.get(entry.selection)
        if current is None or entry.odds_decimal > current:
            best[entry.selection] = entry.odds_decimal
    return best


def _group_key(entry: MarketOdds) -> Tuple[str, str]:
    return (entry.event_id, entry.market)


@app.post("/arbitrage", response_model=ArbResponse)
def evaluate_arbitrage(payload: ArbRequest) -> ArbResponse:
    grouped: Dict[Tuple[str, str], List[MarketOdds]] = {}
    for entry in payload.odds:
        grouped.setdefault(_group_key(entry), []).append(entry)

    opportunities: List[ArbOpportunity] = []
    for (event_id, market), group in grouped.items():
        best_by_selection = _best_odds_by_selection(group)
        implied_sum = sum(1.0 / price for price in best_by_selection.values()) if best_by_selection else 1.0
        has_arb = implied_sum < 1.0
        note = None
        if has_arb:
            note = "Implied probability below 1.0; review manually."
        opportunities.append(
            ArbOpportunity(
                event_id=event_id,
                market=market,
                implied_prob_sum=round(implied_sum, 6),
                has_arb=has_arb,
                notes=note,
            )
        )

    return ArbResponse(opportunities=opportunities, evaluated_at=datetime.utcnow())