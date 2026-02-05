from __future__ import annotations

import os
from datetime import datetime
from typing import List

import httpx
from fastapi import FastAPI, HTTPException

from shared.schemas import (
    ArbRequest,
    DecisionRequest,
    HealthResponse,
    MarketOdds,
    OddsIngestResponse,
    SlackNotification,
)

SERVICE_NAME = os.getenv("SERVICE_NAME", "odds_ingest")
ARB_MATH_URL = os.getenv("ARB_MATH_URL", "http://arb_math:8000")
DECISION_GATEWAY_URL = os.getenv("DECISION_GATEWAY_URL", "http://decision_gateway:8000")
SLACK_NOTIFIER_URL = os.getenv("SLACK_NOTIFIER_URL", "http://slack_notifier:8000")

app = FastAPI(title="Odds Ingest", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.post("/odds", response_model=OddsIngestResponse)
def ingest_odds(payload: List[MarketOdds]) -> OddsIngestResponse:
    accepted = len(payload)
    return OddsIngestResponse(accepted=accepted, dropped=0)


@app.post("/process", response_model=OddsIngestResponse)
def process_odds(payload: List[MarketOdds]) -> OddsIngestResponse:
    if not payload:
        return OddsIngestResponse(accepted=0, dropped=0)

    arb_request = ArbRequest(odds=payload)

    try:
        with httpx.Client(timeout=10.0) as client:
            arb_response = client.post(
                f"{ARB_MATH_URL}/arbitrage",
                json=arb_request.model_dump(),
            )
            arb_response.raise_for_status()
            arb_data = arb_response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="arb_math request failed") from exc

    opportunities = arb_data.get("opportunities", [])
    positives = [opp for opp in opportunities if opp.get("has_arb")]

    if not positives:
        return OddsIngestResponse(accepted=len(payload), dropped=0)

    actionable = []
    for opp in positives:
        try:
            with httpx.Client(timeout=10.0) as client:
                decision_response = client.post(
                    f"{DECISION_GATEWAY_URL}/decision",
                    json=DecisionRequest(opportunity=opp, context={}).model_dump(),
                )
                decision_response.raise_for_status()
                decision_data = decision_response.json()
                decision = decision_data.get("decision", "manual_review")
                rationale = decision_data.get("rationale", "No rationale provided.")
        except Exception:
            decision = "manual_review"
            rationale = "Decision gateway unavailable; manual review."

        if decision not in {"ignore", "dismiss"}:
            actionable.append((opp, decision, rationale))

    for opp, decision, rationale in actionable:
        message = (
            f"Arb opportunity detected for event={opp.get('event_id')} "
            f"market={opp.get('market')} "
            f"implied_sum={opp.get('implied_prob_sum')} "
            f"decision={decision} "
            f"rationale={rationale}"
        )
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    f"{SLACK_NOTIFIER_URL}/notify",
                    json=SlackNotification(message=message).model_dump(),
                )
        except Exception:
            pass

    return OddsIngestResponse(accepted=len(payload), dropped=0)
