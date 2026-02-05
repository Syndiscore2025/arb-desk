from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, HTTPException

from shared.schemas import (
    ArbAlert,
    ArbOpportunity,
    BetCommand,
    HealthResponse,
    SlackNotification,
    SlackNotificationResponse,
)

logger = logging.getLogger(__name__)

SERVICE_NAME = os.getenv("SERVICE_NAME", "slack_notifier")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL")
MARKET_FEED_URL = os.getenv("MARKET_FEED_URL", "http://market_feed:8000")

# In-memory store for pending alerts (for bet command matching)
# In production, use Redis or database
_pending_alerts: Dict[str, ArbAlert] = {}

# Tier emoji mapping
TIER_EMOJI = {
    "fire": "🔥🔥🔥",
    "lightning": "⚡⚡",
    "info": "ℹ️",
}

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


# ─────────────────────────────────────────────────────────────────────────────
# Tiered Arb Alerts
# ─────────────────────────────────────────────────────────────────────────────


def _format_arb_alert(alert: ArbAlert) -> str:
    """Format an arb alert with tier emoji and bet details."""
    opp = alert.opportunity
    tier_emoji = TIER_EMOJI.get(alert.tier, "📊")

    # Header
    lines = [
        f"{tier_emoji} *{opp.profit_percentage:.2f}% ARBITRAGE DETECTED*",
        f"",
        f"*Event:* {opp.event_id}",
        f"*Market:* {opp.market}",
        f"*Implied Sum:* {opp.implied_prob_sum:.4f}",
    ]

    if opp.is_live:
        lines.insert(1, "🔴 *LIVE EVENT*")

    lines.append("")
    lines.append("*📋 Bet Breakdown:*")

    for leg in opp.legs:
        bookmaker = leg.get("bookmaker", "Unknown")
        selection = leg.get("selection", "Unknown")
        odds = leg.get("odds_decimal", 0)
        stake = leg.get("stake", 0)
        payout = leg.get("payout", 0)

        deep_link = alert.deep_links.get(bookmaker, "")

        if deep_link:
            lines.append(f"• *{bookmaker}*: <{deep_link}|{selection}> @ {odds:.2f}")
        else:
            lines.append(f"• *{bookmaker}*: {selection} @ {odds:.2f}")
        lines.append(f"  💰 Stake: ${stake:.2f} → Payout: ${payout:.2f}")

    # Profit summary
    total_stake = sum(leg.get("stake", 0) for leg in opp.legs)
    guaranteed_payout = opp.legs[0].get("payout", 0) if opp.legs else 0
    profit = guaranteed_payout - total_stake

    lines.append("")
    lines.append(f"*💵 Total Stake:* ${total_stake:.2f}")
    lines.append(f"*💸 Guaranteed Profit:* ${profit:.2f}")

    # Bet command instructions
    lines.append("")
    lines.append("─────────────────────────────")
    lines.append(f"💬 *Reply with stake amount to place bets:*")
    lines.append(f"   Example: `bet {alert.alert_id[:8]} 100` for $100 total stake")

    return "\n".join(lines)


@app.post("/alert/arb")
async def send_arb_alert(opportunity: ArbOpportunity) -> SlackNotificationResponse:
    """
    Send a tiered arb alert to Slack.

    Creates an alert with bet details and stores it for bet command processing.
    """
    # Determine tier
    profit = opportunity.profit_percentage or 0
    if profit >= 3.0:
        tier = "fire"
    elif profit >= 1.5:
        tier = "lightning"
    else:
        tier = "info"

    # Generate deep links (placeholder - would be filled by actual book URLs)
    deep_links = {}
    for leg in opportunity.legs:
        bookmaker = leg.get("bookmaker", "")
        event_id = leg.get("event_id", "")
        # Placeholder URLs - real implementation would build actual deep links
        deep_links[bookmaker] = f"https://{bookmaker.lower()}.com/event/{event_id}"

    # Create alert
    alert = ArbAlert(
        alert_id=str(uuid.uuid4()),
        opportunity=opportunity,
        tier=tier,
        message="",  # Will be formatted
        deep_links=deep_links,
        expires_at=datetime.utcnow() + timedelta(minutes=5),  # 5 min expiry
    )

    # Format message
    alert.message = _format_arb_alert(alert)

    # Store for bet command processing
    _pending_alerts[alert.alert_id] = alert

    # Clean up old alerts (>30 min)
    _cleanup_old_alerts()

    # Send to Slack
    notification = SlackNotification(
        message=alert.message,
        channel=DEFAULT_CHANNEL,
        username="ArbDesk Bot",
    )

    return notify(notification)


def _cleanup_old_alerts() -> None:
    """Remove alerts older than 30 minutes."""
    cutoff = datetime.utcnow() - timedelta(minutes=30)
    expired = [
        aid for aid, alert in _pending_alerts.items()
        if alert.created_at < cutoff
    ]
    for aid in expired:
        del _pending_alerts[aid]


# ─────────────────────────────────────────────────────────────────────────────
# Bet Command Handler (Slack Interactive)
# ─────────────────────────────────────────────────────────────────────────────


@app.post("/bet/command")
async def handle_bet_command(command: BetCommand) -> Dict:
    """
    Handle a bet command from a Slack user.

    User replies to an alert with stake amount, we execute bets on their behalf.
    """
    alert = _pending_alerts.get(command.alert_id)

    if not alert:
        # Try partial match (first 8 chars)
        for aid, a in _pending_alerts.items():
            if aid.startswith(command.alert_id) or command.alert_id.startswith(aid[:8]):
                alert = a
                break

    if not alert:
        return {
            "success": False,
            "message": f"Alert {command.alert_id} not found or expired",
        }

    if alert.status != "pending":
        return {
            "success": False,
            "message": f"Alert already {alert.status}",
        }

    # Check expiry
    if alert.expires_at and datetime.utcnow() > alert.expires_at:
        alert.status = "expired"
        return {
            "success": False,
            "message": "Alert has expired. Odds may have changed.",
        }

    # Calculate proportional stakes
    opp = alert.opportunity
    original_total = sum(leg.get("stake", 0) for leg in opp.legs)
    scale_factor = command.stake_amount / original_total if original_total > 0 else 1

    # Place bets via market_feed
    results = []
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            for leg in opp.legs:
                scaled_stake = leg.get("stake", 0) * scale_factor

                bet_request = {
                    "bet_id": str(uuid.uuid4()),
                    "bookmaker": leg.get("bookmaker"),
                    "event_id": leg.get("event_id"),
                    "selection": leg.get("selection"),
                    "odds_decimal": leg.get("odds_decimal"),
                    "stake_amount": round(scaled_stake, 2),
                    "market": leg.get("market"),
                    "sport": leg.get("sport"),
                    "arb_opportunity_id": alert.alert_id,
                }

                response = await client.post(
                    f"{MARKET_FEED_URL}/bet/place",
                    json=bet_request,
                )
                results.append(response.json())

    except Exception as e:
        logger.error(f"Bet placement error: {e}")
        return {
            "success": False,
            "message": f"Error placing bets: {str(e)}",
        }

    # Check results
    all_success = all(r.get("success", False) for r in results)
    alert.status = "accepted" if all_success else "partial"

    # Send confirmation to Slack
    if all_success:
        confirm_msg = f"✅ *Bets Placed Successfully!*\n\nTotal Stake: ${command.stake_amount:.2f}"
    else:
        confirm_msg = f"⚠️ *Partial Bet Placement*\n\nSome bets may have failed. Check results."

    for r in results:
        status = "✅" if r.get("success") else "❌"
        confirm_msg += f"\n{status} {r.get('bookmaker')}: {r.get('confirmation_number', r.get('error', 'Unknown'))}"

    notify(SlackNotification(message=confirm_msg))

    return {
        "success": all_success,
        "message": "Bets placed" if all_success else "Some bets failed",
        "results": results,
    }


@app.post("/slack/events")
async def handle_slack_events(request: Request) -> Dict:
    """
    Handle incoming Slack events (messages, commands).

    Parses bet commands from user messages.
    """
    body = await request.json()

    # Slack challenge verification
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge")}

    event = body.get("event", {})

    # Only process message events
    if event.get("type") != "message":
        return {"ok": True}

    # Ignore bot messages
    if event.get("bot_id"):
        return {"ok": True}

    text = event.get("text", "")
    user_id = event.get("user", "")

    # Parse bet command: "bet <alert_id> <amount>"
    match = re.match(r"bet\s+(\S+)\s+(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if match:
        alert_id = match.group(1)
        stake_amount = float(match.group(2))

        command = BetCommand(
            alert_id=alert_id,
            stake_amount=stake_amount,
            user_id=user_id,
        )

        result = await handle_bet_command(command)
        logger.info(f"Bet command result: {result}")

    return {"ok": True}