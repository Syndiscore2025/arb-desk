from __future__ import annotations

import os
from datetime import datetime

import httpx
from fastapi import FastAPI

from shared.schemas import HealthResponse, SlackNotification, SlackNotificationResponse

SERVICE_NAME = os.getenv("SERVICE_NAME", "slack_notifier")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL")

app = FastAPI(title="Slack Notifier", version="0.1.0")


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(service=SERVICE_NAME, time_utc=datetime.utcnow())


def _webhook_payload(payload: SlackNotification) -> dict:
    data = {"text": payload.message}
    if payload.username:
        data["username"] = payload.username
    return data


def _chat_post_payload(payload: SlackNotification) -> dict:
    return {
        "channel": payload.channel or DEFAULT_CHANNEL,
        "text": payload.message,
    }


@app.post("/notify", response_model=SlackNotificationResponse)
def notify(payload: SlackNotification) -> SlackNotificationResponse:
    if SLACK_WEBHOOK_URL:
        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(SLACK_WEBHOOK_URL, json=_webhook_payload(payload))
                response.raise_for_status()
            return SlackNotificationResponse(delivered=True, detail="Webhook delivered.")
        except Exception:
            return SlackNotificationResponse(delivered=False, detail="Webhook delivery failed.")

    if SLACK_BOT_TOKEN:
        try:
            headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
            with httpx.Client(timeout=10.0) as client:
                response = client.post(
                    "https://slack.com/api/chat.postMessage",
                    json=_chat_post_payload(payload),
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
            delivered = bool(data.get("ok"))
            detail = "Bot token delivered." if delivered else data.get("error", "Slack API error.")
            return SlackNotificationResponse(delivered=delivered, detail=detail)
        except Exception:
            return SlackNotificationResponse(delivered=False, detail="Bot token delivery failed.")

    return SlackNotificationResponse(
        delivered=False,
        detail="No Slack webhook or bot token configured.",
    )