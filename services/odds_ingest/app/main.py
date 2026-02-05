from __future__ import annotations

import os
from datetime import datetime
from typing import List

from fastapi import FastAPI

from shared.schemas import HealthResponse, MarketOdds, OddsIngestResponse

SERVICE_NAME = os.getenv("SERVICE_NAME", "odds_ingest")

app = FastAPI(title="Odds Ingest", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.post("/odds", response_model=OddsIngestResponse)
def ingest_odds(payload: List[MarketOdds]) -> OddsIngestResponse:
    accepted = len(payload)
    return OddsIngestResponse(accepted=accepted, dropped=0)