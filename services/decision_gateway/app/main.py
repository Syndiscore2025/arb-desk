from __future__ import annotations

import os
from datetime import datetime

import httpx
from fastapi import FastAPI

from shared.schemas import DecisionRequest, DecisionResponse, HealthResponse

SERVICE_NAME = os.getenv("SERVICE_NAME", "decision_gateway")
AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

app = FastAPI(title="Decision Gateway", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


@app.post("/decision", response_model=DecisionResponse)
def decision(payload: DecisionRequest) -> DecisionResponse:
    if AI_API_URL and AI_API_KEY:
        try:
            headers = {"Authorization": f"Bearer {AI_API_KEY}"}
            body = {
                "model": AI_MODEL,
                "input": {
                    "opportunity": payload.opportunity.model_dump(),
                    "context": payload.context,
                },
            }
            with httpx.Client(timeout=10.0) as client:
                response = client.post(AI_API_URL, json=body, headers=headers)
                response.raise_for_status()
                data = response.json()

            decision_text = data.get("decision") or "manual_review"
            rationale = data.get("rationale") or "Decision provided by external AI API."
            return DecisionResponse(decision=decision_text, rationale=rationale)
        except Exception:
            return DecisionResponse(
                decision="manual_review",
                rationale="External AI API call failed; manual review required.",
            )

    return DecisionResponse(
        decision="manual_review",
        rationale="No AI API configured; manual review required.",
    )