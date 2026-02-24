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
    """
    Process odds through the full pipeline:
    1. Detect arbitrage opportunities
    2. Detect +EV opportunities
    3. Detect middle opportunities
    4. Route all actionable opportunities through decision_gateway
    5. Send alerts to slack_notifier
    """
    if not payload:
        return OddsIngestResponse(accepted=0, dropped=0)

    arb_request = ArbRequest(odds=payload)
    all_opportunities = []

    # 1. Detect arbitrage opportunities
    try:
        with httpx.Client(timeout=10.0) as client:
            arb_response = client.post(
                f"{ARB_MATH_URL}/arbitrage",
                json=arb_request.model_dump(mode="json"),
            )
            arb_response.raise_for_status()
            arb_data = arb_response.json()
            arb_opps = [opp for opp in arb_data.get("opportunities", []) if opp.get("has_arb")]
            all_opportunities.extend(arb_opps)
    except Exception:
        pass  # Continue with other opportunity types

    # 2. Detect +EV opportunities
    try:
        with httpx.Client(timeout=10.0) as client:
            ev_response = client.post(
                f"{ARB_MATH_URL}/positive-ev",
                json=arb_request.model_dump(mode="json"),
            )
            ev_response.raise_for_status()
            ev_data = ev_response.json()
            ev_opps = ev_data.get("opportunities", [])
            all_opportunities.extend(ev_opps)
    except Exception:
        pass  # Continue with other opportunity types

    # 3. Detect middle opportunities
    try:
        with httpx.Client(timeout=10.0) as client:
            middle_response = client.post(
                f"{ARB_MATH_URL}/middles",
                json=arb_request.model_dump(mode="json"),
            )
            middle_response.raise_for_status()
            middle_data = middle_response.json()
            middle_opps = middle_data.get("opportunities", [])
            all_opportunities.extend(middle_opps)
    except Exception:
        pass  # Continue

    if not all_opportunities:
        return OddsIngestResponse(accepted=len(payload), dropped=0)

    # 4. Route through decision gateway and send alerts
    for opp in all_opportunities:
        opp_type = opp.get("opportunity_type", "arb")

        # Get decision from gateway
        try:
            with httpx.Client(timeout=10.0) as client:
                decision_response = client.post(
                    f"{DECISION_GATEWAY_URL}/decision",
                    json=DecisionRequest(opportunity=opp, context={}).model_dump(mode="json"),
                )
                decision_response.raise_for_status()
                decision_data = decision_response.json()
                decision = decision_data.get("decision", "manual_review")
        except Exception:
            decision = "manual_review"

        # Skip ignored/dismissed opportunities
        if decision in {"ignore", "dismiss"}:
            continue

        # 5. Send alert to Slack via the unified /alert/arb endpoint
        try:
            with httpx.Client(timeout=10.0) as client:
                client.post(
                    f"{SLACK_NOTIFIER_URL}/alert/arb",
                    json=opp,
                )
        except Exception:
            pass

    return OddsIngestResponse(accepted=len(payload), dropped=0)
