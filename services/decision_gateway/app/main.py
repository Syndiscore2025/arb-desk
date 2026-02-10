from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from shared.schemas import DecisionRequest, DecisionResponse, HealthResponse

from .stealth_advisor import StealthAdvisor

SERVICE_NAME = os.getenv("SERVICE_NAME", "decision_gateway")

app = FastAPI(title="Decision Gateway", version="0.1.0")

# Global stealth advisor instance
stealth_advisor = StealthAdvisor()


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Models
# ─────────────────────────────────────────────────────────────────────────────


class HeatScoreResponse(BaseModel):
    """Response containing heat scores for all bookmakers."""
    bookmakers: Dict[str, Dict[str, Any]]
    timestamp: datetime


class RecordBetRequest(BaseModel):
    """Request to record a bet result."""
    bookmaker: str
    is_arb: bool = True
    stake: float
    profit: float = 0.0
    won: bool = True


class CoolingRequest(BaseModel):
    """Request to force cooling on a bookmaker."""
    bookmaker: str
    hours: int = 24


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.post("/decision", response_model=DecisionResponse)
async def decision(payload: DecisionRequest) -> DecisionResponse:
    """
    Evaluate an arb opportunity using the Stealth Advisor.

    The advisor analyzes betting patterns, heat scores, and opportunity quality
    to decide whether to take, skip, or suggest cover bets before the arb.
    """
    return await stealth_advisor.evaluate(payload.opportunity, payload.context)


@app.get("/heat", response_model=HeatScoreResponse)
def get_heat_scores() -> HeatScoreResponse:
    """
    Get current heat scores for all tracked bookmakers.

    Heat score (0-100) indicates how suspicious the betting pattern appears.
    Higher scores indicate more risk of account limiting.
    """
    return HeatScoreResponse(
        bookmakers=stealth_advisor.get_all_heat_scores(),
        timestamp=datetime.utcnow(),
    )


@app.get("/heat/{bookmaker}")
def get_bookmaker_heat(bookmaker: str) -> Dict[str, Any]:
    """Get heat score for a specific bookmaker."""
    all_scores = stealth_advisor.get_all_heat_scores()
    if bookmaker not in all_scores:
        # Return a default profile
        profile = stealth_advisor.get_profile(bookmaker)
        return {
            "bookmaker": bookmaker,
            "heat_score": round(profile.heat_score, 1),
            "win_rate": round(profile.win_rate, 3),
            "total_bets": profile.total_bets,
            "is_new": True,
        }
    return {"bookmaker": bookmaker, **all_scores[bookmaker]}


@app.post("/record-bet")
def record_bet(request: RecordBetRequest) -> Dict[str, Any]:
    """
    Record a bet result to update heat tracking.

    Call this after each bet placement to keep heat scores accurate.
    """
    stealth_advisor.record_bet_result(
        bookmaker=request.bookmaker,
        is_arb=request.is_arb,
        stake=request.stake,
        profit=request.profit,
        won=request.won,
    )
    profile = stealth_advisor.get_profile(request.bookmaker)
    return {
        "bookmaker": request.bookmaker,
        "recorded": True,
        "new_heat_score": round(profile.heat_score, 1),
        "needs_cooling": profile.needs_cooling,
    }


@app.post("/cool")
def force_cooling(request: CoolingRequest) -> Dict[str, Any]:
    """
    Force a bookmaker into a cooling period.

    Use this to manually pause betting on a bookmaker that feels risky.
    """
    stealth_advisor.force_cooling(request.bookmaker, request.hours)
    profile = stealth_advisor.get_profile(request.bookmaker)
    return {
        "bookmaker": request.bookmaker,
        "cooling_started": True,
        "cooling_until": profile.cooling_until.isoformat() if profile.cooling_until else None,
        "hours": request.hours,
    }