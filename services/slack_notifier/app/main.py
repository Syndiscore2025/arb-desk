from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import docker
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
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN")  # xapp- token for Socket Mode
DEFAULT_CHANNEL = os.getenv("SLACK_DEFAULT_CHANNEL")
MARKET_FEED_URL = os.getenv("MARKET_FEED_URL", "http://market_feed:8000")
DECISION_GATEWAY_URL = os.getenv("DECISION_GATEWAY_URL", "http://decision_gateway:8000")
COMPOSE_PROJECT = os.getenv("COMPOSE_PROJECT_NAME", "arb-desk")
ALERT_STATE_PATH = os.getenv("ALERT_STATE_PATH", "/app/data/alert_state.json")

# Docker client for service control
try:
    docker_client = docker.from_env()
except Exception as e:
    logger.warning(f"Docker client unavailable: {e}")
    docker_client = None

# Valid services that can be controlled
CONTROLLABLE_SERVICES = [
    "market_feed",
    "odds_ingest",
    "arb_math",
    "decision_gateway",
    "browser_shadow",
]

# Service health-check URLs (reachable inside Docker network)
SERVICE_HEALTH_URLS = {
    "odds_ingest": "http://odds_ingest:8000/health",
    "arb_math": "http://arb_math:8000/health",
    "browser_shadow": "http://browser_shadow:8000/health",
    "decision_gateway": "http://decision_gateway:8000/health",
    "slack_notifier": "http://localhost:8000/health",
    "market_feed": "http://market_feed:8000/health",
    "postgres": "http://odds_ingest:8000/health",  # proxy: if odds_ingest is up, postgres is up
}

# In-memory store for pending alerts (for bet command matching)
# In production, use Redis or database
_pending_alerts: Dict[str, ArbAlert] = {}

# Tier emoji mapping
TIER_EMOJI = {
    "fire": "🔥🔥🔥",
    "lightning": "⚡⚡",
    "info": "ℹ️",
}

# ─────────────────────────────────────────────────────────────────────────────
# Alert Control Plane + Dedupe/Cooldown/Lifecycle
# ─────────────────────────────────────────────────────────────────────────────

# Global alert enable/disable (env default, runtime-togglable)
ALERTS_ENABLED_DEFAULT = os.getenv("ALERTS_ENABLED", "true").lower() in ("1", "true", "yes")

# Dedupe cooldown window in seconds (env-configurable)
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "300"))

# Minimum quality gates (env-configurable, per opportunity type)
MIN_ARB_PROFIT_PCT = float(os.getenv("MIN_ARB_PROFIT_PCT", "0.5"))
MIN_EV_PCT = float(os.getenv("MIN_EV_PCT", "1.0"))
MIN_MIDDLE_GAP = float(os.getenv("MIN_MIDDLE_GAP", "0.5"))

# Max alerts per minute (rate limiter)
ALERT_RATE_LIMIT_PER_MINUTE = int(os.getenv("ALERT_RATE_LIMIT_PER_MINUTE", "10"))

# Runtime alert state — all in-memory, survives across requests but not restarts
_alert_state: Dict[str, Any] = {
    "enabled": ALERTS_ENABLED_DEFAULT,
    "disabled_at": None,
    "disabled_by": None,
}

# Dedupe: fingerprint -> last_sent_at
_alert_dedupe: Dict[str, datetime] = {}

# Lifecycle tracking: fingerprint -> stats
_alert_lifecycle: Dict[str, Dict[str, Any]] = {}

# Rate limiter: list of send timestamps in the last 60s
_alert_send_times: List[datetime] = []

# Counters for observability
_alert_stats: Dict[str, int] = {
    "total_received": 0,
    "total_sent": 0,
    "suppressed_muted": 0,
    "suppressed_dedupe": 0,
    "suppressed_quality": 0,
    "suppressed_rate_limit": 0,
}


def _alert_edge(opp: ArbOpportunity) -> float:
    """Return the best available edge metric for an opportunity."""
    return float(opp.profit_percentage or opp.ev_percentage or opp.middle_gap or 0.0)


def _save_alert_state() -> None:
    """Persist alert control state so mute/dedupe survive restarts."""
    try:
        directory = os.path.dirname(ALERT_STATE_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)

        payload = {
            "alert_state": dict(_alert_state),
            "alert_dedupe": {fp: ts.isoformat() for fp, ts in _alert_dedupe.items()},
            "alert_lifecycle": dict(_alert_lifecycle),
            "alert_stats": dict(_alert_stats),
            "saved_at": datetime.utcnow().isoformat(),
        }
        with open(ALERT_STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
    except Exception as exc:
        logger.warning(f"Failed to save alert state: {exc}")


def _load_alert_state() -> None:
    """Load persisted alert control state if it exists."""
    if not os.path.exists(ALERT_STATE_PATH):
        return

    try:
        with open(ALERT_STATE_PATH, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        stored_state = payload.get("alert_state") or {}
        if stored_state:
            _alert_state.update({
                "enabled": stored_state.get("enabled", _alert_state["enabled"]),
                "disabled_at": stored_state.get("disabled_at"),
                "disabled_by": stored_state.get("disabled_by"),
            })

        _alert_dedupe.clear()
        for fp, ts in (payload.get("alert_dedupe") or {}).items():
            try:
                _alert_dedupe[fp] = datetime.fromisoformat(ts)
            except Exception:
                continue

        _alert_lifecycle.clear()
        _alert_lifecycle.update(payload.get("alert_lifecycle") or {})

        for key, value in (payload.get("alert_stats") or {}).items():
            if key in _alert_stats:
                _alert_stats[key] = int(value)

        _cleanup_stale_dedupe()
        logger.info(
            "Loaded alert state: enabled=%s dedupe=%s lifecycle=%s",
            _alert_state["enabled"],
            len(_alert_dedupe),
            len(_alert_lifecycle),
        )
    except Exception as exc:
        logger.warning(f"Failed to load alert state: {exc}")


def _alert_fingerprint(opp: ArbOpportunity) -> str:
    """Compute a stable fingerprint for deduplication."""
    parts = [
        opp.event_id,
        opp.market,
        opp.opportunity_type or "arb",
    ]
    for leg in sorted(opp.legs, key=lambda l: l.get("bookmaker", "")):
        parts.append(leg.get("bookmaker", ""))
        parts.append(leg.get("selection", ""))
        # Round odds to 2 decimals so tiny fluctuations don't break dedupe
        parts.append(f"{leg.get('odds_decimal', 0):.2f}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _prune_rate_limit_window() -> None:
    """Remove send timestamps older than 60 seconds."""
    cutoff = datetime.utcnow() - timedelta(seconds=60)
    while _alert_send_times and _alert_send_times[0] < cutoff:
        _alert_send_times.pop(0)


def _should_suppress_alert(opp: ArbOpportunity) -> Optional[str]:
    """
    Check whether an alert should be suppressed.
    Returns a reason string if suppressed, None if it should be sent.
    """
    # 1. Global mute
    if not _alert_state["enabled"]:
        _alert_stats["suppressed_muted"] += 1
        return "alerts_disabled"

    # 2. Quality gate
    opp_type = opp.opportunity_type or "arb"
    if opp_type == "arb":
        profit = opp.profit_percentage or 0
        if profit < MIN_ARB_PROFIT_PCT:
            _alert_stats["suppressed_quality"] += 1
            return f"arb_profit_{profit:.2f}_below_{MIN_ARB_PROFIT_PCT}"
    elif opp_type == "positive_ev":
        ev = opp.ev_percentage or 0
        if ev < MIN_EV_PCT:
            _alert_stats["suppressed_quality"] += 1
            return f"ev_{ev:.2f}_below_{MIN_EV_PCT}"
    elif opp_type == "middle":
        gap = opp.middle_gap or 0
        if gap < MIN_MIDDLE_GAP:
            _alert_stats["suppressed_quality"] += 1
            return f"middle_gap_{gap:.2f}_below_{MIN_MIDDLE_GAP}"

    # 3. Dedupe/cooldown
    fp = _alert_fingerprint(opp)
    last_sent = _alert_dedupe.get(fp)
    if last_sent:
        elapsed = (datetime.utcnow() - last_sent).total_seconds()
        if elapsed < ALERT_COOLDOWN_SECONDS:
            _alert_stats["suppressed_dedupe"] += 1
            return f"dedupe_cooldown_{int(elapsed)}s_of_{ALERT_COOLDOWN_SECONDS}s"

    # 4. Rate limit
    _prune_rate_limit_window()
    if len(_alert_send_times) >= ALERT_RATE_LIMIT_PER_MINUTE:
        _alert_stats["suppressed_rate_limit"] += 1
        return f"rate_limit_{len(_alert_send_times)}_per_min"

    return None


def _record_alert_sent(opp: ArbOpportunity) -> None:
    """Record that an alert was sent for lifecycle tracking."""
    fp = _alert_fingerprint(opp)
    now = datetime.utcnow()
    _alert_dedupe[fp] = now
    _alert_send_times.append(now)

    if fp not in _alert_lifecycle:
        _alert_lifecycle[fp] = {
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "send_count": 0,
            "suppressed_count": 0,
            "best_edge": 0.0,
            "event_id": opp.event_id,
            "market": opp.market,
            "type": opp.opportunity_type or "arb",
        }
    entry = _alert_lifecycle[fp]
    entry["last_seen"] = now.isoformat()
    entry["send_count"] += 1

    # Track best edge seen
    edge = _alert_edge(opp)
    if edge > entry["best_edge"]:
        entry["best_edge"] = edge

    _save_alert_state()


def _record_alert_suppressed(opp: ArbOpportunity) -> None:
    """Record that an alert was suppressed."""
    fp = _alert_fingerprint(opp)
    now = datetime.utcnow()
    if fp not in _alert_lifecycle:
        _alert_lifecycle[fp] = {
            "first_seen": now.isoformat(),
            "last_seen": now.isoformat(),
            "send_count": 0,
            "suppressed_count": 0,
            "best_edge": 0.0,
            "event_id": opp.event_id,
            "market": opp.market,
            "type": opp.opportunity_type or "arb",
        }
    entry = _alert_lifecycle[fp]
    entry["last_seen"] = now.isoformat()
    entry["suppressed_count"] += 1
    edge = _alert_edge(opp)
    if edge > entry["best_edge"]:
        entry["best_edge"] = edge

    _save_alert_state()


def _cleanup_stale_dedupe() -> None:
    """Remove dedupe entries older than 2x the cooldown window."""
    cutoff = datetime.utcnow() - timedelta(seconds=ALERT_COOLDOWN_SECONDS * 2)
    stale = [fp for fp, ts in _alert_dedupe.items() if ts < cutoff]
    for fp in stale:
        del _alert_dedupe[fp]

    # Also prune lifecycle entries older than 1 hour
    lf_cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    stale_lf = [fp for fp, e in _alert_lifecycle.items() if e["last_seen"] < lf_cutoff]
    for fp in stale_lf:
        del _alert_lifecycle[fp]

    if stale or stale_lf:
        _save_alert_state()

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


def _format_positive_ev_alert(alert: ArbAlert) -> str:
    """Format a +EV alert with edge details."""
    opp = alert.opportunity

    # Get main details from first leg
    leg = opp.legs[0] if opp.legs else {}
    bookmaker = leg.get("bookmaker", "Unknown")
    selection = leg.get("selection", "Unknown")
    odds = leg.get("odds_decimal", 0)
    fair_odds = leg.get("fair_odds", 0)
    ev_pct = opp.ev_percentage or 0

    lines = [
        f"📈 *+{ev_pct:.1f}% POSITIVE EV*",
        "",
        f"*Event:* {opp.event_id}",
        f"*Market:* {opp.market}",
        "",
        f"*Book:* {bookmaker}",
        f"*Selection:* {selection}",
        f"*Offered Odds:* {odds:.3f}",
        f"*Fair Odds:* {fair_odds:.3f}",
        "",
        f"*True Probability:* {opp.true_probability*100:.1f}%" if opp.true_probability else "",
        f"*Kelly Fraction:* {opp.kelly_fraction*100:.1f}% of bankroll" if opp.kelly_fraction else "",
    ]

    # Filter out empty lines
    lines = [l for l in lines if l]

    lines.append("")
    lines.append("─────────────────────────────")
    lines.append("_+EV bets are profitable long-term, not guaranteed per bet._")

    return "\n".join(lines)


def _format_middle_alert(alert: ArbAlert) -> str:
    """Format a middle opportunity alert."""
    opp = alert.opportunity

    lines = [
        f"🎯 *MIDDLE OPPORTUNITY*",
        "",
        f"*Event:* {opp.event_id}",
        f"*Type:* {opp.market}",
        "",
        f"*Middle Range:* {opp.middle_range}",
        f"*Gap:* {opp.middle_gap:.1f} points",
        f"*Hit Probability:* ~{opp.middle_probability*100:.0f}%" if opp.middle_probability else "",
        "",
        "*📋 Legs:*",
    ]

    for leg in opp.legs:
        bookmaker = leg.get("bookmaker", "Unknown")
        selection = leg.get("selection", "Unknown")
        odds = leg.get("odds_decimal", 0)
        line = leg.get("line", "")
        lines.append(f"• *{bookmaker}*: {selection} ({line:+.1f}) @ {odds:.2f}")

    lines.append("")
    lines.append("─────────────────────────────")
    lines.append("_Middles: worst case lose vig, best case win both sides._")

    return "\n".join(lines)


@app.post("/alert/arb")
async def send_arb_alert(opportunity: ArbOpportunity) -> SlackNotificationResponse:
    """
    Send an opportunity alert to Slack.

    Handles all opportunity types: arb, positive_ev, middle.
    Creates an alert with details and stores it for bet command processing.
    """
    opp_type = opportunity.opportunity_type or "arb"

    # Determine tier based on opportunity type
    if opp_type == "arb":
        profit = opportunity.profit_percentage or 0
        if profit >= 3.0:
            tier = "fire"
        elif profit >= 1.5:
            tier = "lightning"
        else:
            tier = "info"
    elif opp_type == "positive_ev":
        ev = opportunity.ev_percentage or 0
        if ev >= 5.0:
            tier = "fire"
        elif ev >= 3.0:
            tier = "lightning"
        else:
            tier = "info"
    elif opp_type == "middle":
        gap = opportunity.middle_gap or 0
        if gap >= 3.0:
            tier = "fire"
        elif gap >= 1.5:
            tier = "lightning"
        else:
            tier = "info"
    else:
        tier = "info"

    # Generate deep links (placeholder - would be filled by actual book URLs)
    deep_links = {}
    for leg in opportunity.legs:
        bookmaker = leg.get("bookmaker", "")
        event_id = leg.get("event_id", "")
        deep_links[bookmaker] = f"https://{bookmaker.lower()}.com/event/{event_id}"

    # Create alert
    alert = ArbAlert(
        alert_id=str(uuid.uuid4()),
        opportunity=opportunity,
        tier=tier,
        message="",  # Will be formatted
        deep_links=deep_links,
        expires_at=datetime.utcnow() + timedelta(minutes=5),
    )

    # Format message based on opportunity type
    if opp_type == "positive_ev":
        alert.message = _format_positive_ev_alert(alert)
    elif opp_type == "middle":
        alert.message = _format_middle_alert(alert)
    else:
        alert.message = _format_arb_alert(alert)

    # Store for bet command processing (always, even if suppressed — so bet commands still work)
    _pending_alerts[alert.alert_id] = alert

    # Clean up old alerts (>30 min) and stale dedupe entries
    _cleanup_old_alerts()
    _cleanup_stale_dedupe()

    # ── Control plane: check whether to suppress this alert ──
    _alert_stats["total_received"] += 1
    suppress_reason = _should_suppress_alert(opportunity)
    if suppress_reason:
        _record_alert_suppressed(opportunity)
        logger.info(f"Alert suppressed: {suppress_reason} | {opportunity.event_id}")
        return SlackNotificationResponse(
            delivered=False,
            detail=f"Suppressed: {suppress_reason}",
        )

    # Send to Slack
    notification = SlackNotification(
        message=alert.message,
        channel=DEFAULT_CHANNEL,
        username="ArbDesk Bot",
    )

    result = notify(notification)
    if result.delivered:
        _record_alert_sent(opportunity)
        _alert_stats["total_sent"] += 1
    return result


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
# Alert Control Plane Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/alerts/status")
def alerts_status() -> Dict[str, Any]:
    """Get current alert control plane state, stats, and lifecycle."""
    return {
        "enabled": _alert_state["enabled"],
        "disabled_at": _alert_state["disabled_at"],
        "disabled_by": _alert_state["disabled_by"],
        "config": {
            "cooldown_seconds": ALERT_COOLDOWN_SECONDS,
            "rate_limit_per_minute": ALERT_RATE_LIMIT_PER_MINUTE,
            "min_arb_profit_pct": MIN_ARB_PROFIT_PCT,
            "min_ev_pct": MIN_EV_PCT,
            "min_middle_gap": MIN_MIDDLE_GAP,
        },
        "stats": dict(_alert_stats),
        "dedupe_entries": len(_alert_dedupe),
        "lifecycle_entries": len(_alert_lifecycle),
        "pending_alerts": len(_pending_alerts),
    }


@app.post("/alerts/enable")
def alerts_enable() -> Dict[str, Any]:
    """Enable alert delivery."""
    _alert_state["enabled"] = True
    _alert_state["disabled_at"] = None
    _alert_state["disabled_by"] = None
    _save_alert_state()
    logger.info("Alerts ENABLED via API")
    return {"enabled": True, "message": "Alerts enabled"}


@app.post("/alerts/disable")
def alerts_disable() -> Dict[str, Any]:
    """Disable alert delivery (mute). Operator/2FA messages still go through."""
    _alert_state["enabled"] = False
    _alert_state["disabled_at"] = datetime.utcnow().isoformat()
    _alert_state["disabled_by"] = "api"
    _save_alert_state()
    logger.info("Alerts DISABLED via API")
    return {"enabled": False, "message": "Alerts disabled (muted). Operator messages still delivered."}


@app.get("/alerts/history")
def alerts_history() -> Dict[str, Any]:
    """Get recent alert lifecycle data."""
    # Sort by last_seen descending
    sorted_entries = sorted(
        _alert_lifecycle.items(),
        key=lambda x: x[1]["last_seen"],
        reverse=True,
    )[:50]
    return {
        "count": len(sorted_entries),
        "entries": [{"fingerprint": fp, **data} for fp, data in sorted_entries],
    }


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

    # Parse 2FA code submission: "2fa <request_id> <code>"
    twofa_match = re.match(r"2fa\s+(\S+)\s+(\d{4,8})", text, re.IGNORECASE)
    if twofa_match:
        request_id_prefix = twofa_match.group(1)
        code = twofa_match.group(2)

        result = await handle_2fa_submission(request_id_prefix, code, user_id)
        logger.info(f"2FA submission result: {result}")
        return {"ok": True}

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

    # Parse visual login command: "arb login visual <bookmaker>"
    visual_login_match = re.match(
        r"arb\s+login\s+visual\s+(\S+)",
        text,
        re.IGNORECASE,
    )
    if visual_login_match:
        bookmaker = visual_login_match.group(1)
        result = await handle_visual_login_command(bookmaker, user_id)
        logger.info(f"Visual login command result: {result}")
        return {"ok": True}

    # Parse service control commands: "arb start|stop|restart|status|logs|heat|cool|login|mute|unmute|alerts [service]"
    arb_match = re.match(
        r"arb\s+(start|stop|restart|status|scrape|logs|heat|cool|login|mute|unmute|alerts)(?:\s+(\S+))?",
        text,
        re.IGNORECASE,
    )
    if arb_match:
        action = arb_match.group(1).lower()
        arg = arb_match.group(2)

        if action == "logs":
            result = await handle_logs_command(arg, user_id)
        elif action == "heat":
            result = await handle_heat_command(arg, user_id)
        elif action == "cool":
            result = await handle_cool_command(arg, user_id)
        elif action == "login":
            result = await handle_login_command(arg, user_id)
        elif action in ("mute", "unmute", "alerts"):
            result = await handle_alert_control_command(action, user_id)
        else:
            result = await handle_service_control(action, arg, user_id)
        logger.info(f"Command result: {result}")

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# Log Viewing Commands
# ─────────────────────────────────────────────────────────────────────────────


async def handle_logs_command(log_type: Optional[str], user_id: str) -> Dict:
    """
    Handle log viewing commands from Slack.

    Usage:
        arb logs           - Get recent logs (last 50 lines)
        arb logs errors    - Get only ERROR/CRITICAL logs
        arb logs browser   - Get browser-specific logs
        arb logs summary   - Get log statistics summary
    """
    log_type = (log_type or "recent").lower()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if log_type == "errors":
                response = await client.get(f"{MARKET_FEED_URL}/logs/errors", params={"lines": 20})
                title = "🔴 Recent Errors"
            elif log_type == "browser":
                response = await client.get(f"{MARKET_FEED_URL}/logs/browser", params={"lines": 30})
                title = "🌐 Browser Logs"
            elif log_type == "summary":
                response = await client.get(f"{MARKET_FEED_URL}/logs/summary")
                data = response.json()

                # Format summary nicely
                lines = ["📊 *Log Summary*", ""]
                lines.append(f"*Total entries:* {data.get('total_entries', 0)}")
                lines.append("")

                # Level counts
                lines.append("*By Level:*")
                for level, count in data.get("level_counts", {}).items():
                    if count > 0:
                        lines.append(f"  • {level}: {count}")

                # Bookmaker counts
                if data.get("bookmaker_counts"):
                    lines.append("")
                    lines.append("*By Bookmaker:*")
                    for bm, count in data.get("bookmaker_counts", {}).items():
                        lines.append(f"  • {bm}: {count}")

                # Recent errors
                if data.get("recent_errors"):
                    lines.append("")
                    lines.append("*Recent Errors:*")
                    for err in data.get("recent_errors", [])[:5]:
                        lines.append(f"  ⚠️ [{err.get('bookmaker', 'unknown')}] {err.get('message', '')[:100]}")

                msg = "\n".join(lines)
                notify(SlackNotification(message=msg))
                return {"success": True, "message": msg}
            else:
                # Default: recent logs
                response = await client.get(f"{MARKET_FEED_URL}/logs", params={"lines": 30})
                title = "📋 Recent Logs"

            # Format log output
            logs_text = response.text.strip()
            if not logs_text or logs_text == "No logs available yet.":
                msg = f"{title}\n\n_No logs available yet._"
            else:
                # Truncate if too long for Slack
                if len(logs_text) > 2500:
                    logs_text = logs_text[-2500:]
                    logs_text = "...\n" + logs_text
                msg = f"{title}\n```\n{logs_text}\n```"

            notify(SlackNotification(message=msg))
            return {"success": True, "message": msg}

    except Exception as e:
        msg = f"❌ Failed to fetch logs: {str(e)}"
        notify(SlackNotification(message=msg))
        return {"success": False, "message": msg}


# ─────────────────────────────────────────────────────────────────────────────
# Service Control via Docker
# ─────────────────────────────────────────────────────────────────────────────


def _get_container(service_name: str):
    """Get Docker container for a service."""
    if not docker_client:
        return None

    container_name = f"{COMPOSE_PROJECT}-{service_name}-1"
    try:
        return docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        # Try alternate naming conventions
        for container in docker_client.containers.list(all=True):
            if service_name in container.name:
                return container
    except Exception as e:
        logger.error(f"Error getting container {service_name}: {e}")
    return None


def _get_all_containers() -> Dict[str, dict]:
    """Get status of all ArbDesk containers."""
    if not docker_client:
        return {}

    result = {}
    try:
        for container in docker_client.containers.list(all=True):
            # Match containers from our compose project
            if COMPOSE_PROJECT in container.name or any(
                svc in container.name for svc in CONTROLLABLE_SERVICES + ["postgres"]
            ):
                # Extract service name
                name = container.name
                for svc in CONTROLLABLE_SERVICES + ["postgres", "slack_notifier"]:
                    if svc in name:
                        result[svc] = {
                            "status": container.status,
                            "id": container.short_id,
                        }
                        break
    except Exception as e:
        logger.error(f"Error listing containers: {e}")

    return result


async def _check_service_health(service_name: str, url: str) -> tuple:
    """Check a single service's health via HTTP. Returns (name, status, detail)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return (service_name, "running", data.get("time_utc", ""))
            else:
                return (service_name, "unhealthy", f"HTTP {response.status_code}")
    except Exception:
        return (service_name, "down", "unreachable")


async def handle_service_control(action: str, service: Optional[str], user_id: str) -> Dict:
    """Handle service control commands from Slack."""

    # Status command - uses HTTP health checks (no Docker socket needed)
    if action == "status":
        import asyncio
        tasks = [
            _check_service_health(svc, url)
            for svc, url in sorted(SERVICE_HEALTH_URLS.items())
            if svc != "postgres"  # skip proxy check
        ]
        results = await asyncio.gather(*tasks)

        lines = ["📊 *ArbDesk Service Status*", ""]
        for svc, status, detail in sorted(results, key=lambda x: x[0]):
            if status == "running":
                emoji = "🟢"
            elif status == "unhealthy":
                emoji = "🟡"
            else:
                emoji = "🔴"
            lines.append(f"{emoji} *{svc}*: {status}")
        msg = "\n".join(lines)

        notify(SlackNotification(message=msg))
        return {"success": True, "message": msg}

    # Scrape command - trigger market feed scrape
    if action == "scrape":
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(f"{MARKET_FEED_URL}/scrape-all")
                data = response.json()
            msg = f"🔄 *Scrape triggered*\n{json.dumps(data, indent=2)}"
        except Exception as e:
            msg = f"❌ Scrape failed: {str(e)}"

        notify(SlackNotification(message=msg))
        return {"success": True, "message": msg}

    # Start/stop/restart require Docker socket
    if action in ("start", "stop", "restart"):
        if not docker_client:
            msg = (
                f"❌ `arb {action}` requires Docker socket access (not available on Windows Docker Desktop).\n"
                f"Use Docker Desktop or run `docker compose restart {service or '<service>'}` from your terminal instead."
            )
            notify(SlackNotification(message=msg))
            return {"success": False, "message": msg}

        if not service:
            msg = f"❌ Please specify a service: `arb {action} <service>`\n\nAvailable: {', '.join(CONTROLLABLE_SERVICES)}"
            notify(SlackNotification(message=msg))
            return {"success": False, "message": msg}

        service = service.lower()
        if service not in CONTROLLABLE_SERVICES:
            msg = f"❌ Unknown service: `{service}`\n\nAvailable: {', '.join(CONTROLLABLE_SERVICES)}"
            notify(SlackNotification(message=msg))
            return {"success": False, "message": msg}

        container = _get_container(service)
        if not container:
            msg = f"❌ Container for `{service}` not found."
            notify(SlackNotification(message=msg))
            return {"success": False, "message": msg}

        try:
            if action == "start":
                container.start()
                msg = f"✅ Started `{service}`"
            elif action == "stop":
                container.stop(timeout=10)
                msg = f"🛑 Stopped `{service}`"
            elif action == "restart":
                container.restart(timeout=10)
                msg = f"🔄 Restarted `{service}`"
            else:
                msg = f"❌ Unknown action: `{action}`"
        except Exception as e:
            msg = f"❌ Failed to {action} `{service}`: {str(e)}"

        notify(SlackNotification(message=msg))
        return {"success": True, "message": msg}

    msg = f"❌ Unknown action: `{action}`"
    notify(SlackNotification(message=msg))
    return {"success": False, "message": msg}


@app.get("/services/status")
async def get_services_status() -> Dict:
    """Get status of all ArbDesk services."""
    if not docker_client:
        raise HTTPException(status_code=503, detail="Docker not available")

    return {"services": _get_all_containers()}


@app.post("/services/{service}/{action}")
async def control_service(service: str, action: str) -> Dict:
    """Control a service via API."""
    if action not in ["start", "stop", "restart"]:
        raise HTTPException(status_code=400, detail=f"Invalid action: {action}")

    return await handle_service_control(action, service, "api")


# ─────────────────────────────────────────────────────────────────────────────
# Stealth Heat Commands
# ─────────────────────────────────────────────────────────────────────────────


async def handle_heat_command(bookmaker: Optional[str], user_id: str) -> Dict:
    """
    Handle heat score viewing commands from Slack.

    Usage:
        arb heat           - Get heat scores for all bookmakers
        arb heat fanduel   - Get heat score for specific bookmaker
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            if bookmaker:
                response = await client.get(f"{DECISION_GATEWAY_URL}/heat/{bookmaker}")
                response.raise_for_status()
                data = response.json()

                # Format single bookmaker heat
                heat = data.get("heat_score", 0)
                heat_emoji = "🔥" if heat > 60 else "🟡" if heat > 40 else "🟢"

                lines = [
                    f"{heat_emoji} *Heat Score for {bookmaker.upper()}*",
                    "",
                    f"*Heat Score:* {heat}/100",
                    f"*Win Rate:* {data.get('win_rate', 0):.1%}",
                    f"*Total Bets:* {data.get('total_bets', 0)}",
                    f"*Arb Bets Today:* {data.get('arb_bets_today', 0)}",
                    f"*Consecutive Wins:* {data.get('consecutive_wins', 0)}",
                ]

                if data.get("needs_cooling"):
                    lines.append("")
                    lines.append("⚠️ *COOLING REQUIRED* - Account at risk!")

                if data.get("cooling_until"):
                    lines.append(f"*Cooling Until:* {data.get('cooling_until')}")

                message = "\n".join(lines)
            else:
                response = await client.get(f"{DECISION_GATEWAY_URL}/heat")
                response.raise_for_status()
                data = response.json()
                bookmakers = data.get("bookmakers", {})

                if not bookmakers:
                    message = "📊 No bookmaker heat data yet. Start placing bets to track heat."
                else:
                    lines = ["🌡️ *Bookmaker Heat Scores*", ""]

                    for bm, info in sorted(bookmakers.items()):
                        heat = info.get("heat_score", 0)
                        heat_emoji = "🔥" if heat > 60 else "🟡" if heat > 40 else "🟢"
                        cooling = " 🧊 COOLING" if info.get("needs_cooling") else ""
                        lines.append(
                            f"{heat_emoji} *{bm}*: {heat:.0f}/100 "
                            f"(WR: {info.get('win_rate', 0):.0%}, "
                            f"Bets: {info.get('total_bets', 0)}, "
                            f"Wins: {info.get('consecutive_wins', 0)}){cooling}"
                        )

                    lines.append("")
                    lines.append("_Use `arb cool <bookmaker>` to force a cooling period._")
                    message = "\n".join(lines)

            await _send_slack_response(message, user_id)
            return {"ok": True, "sent": True}

    except Exception as e:
        logger.error(f"Heat command failed: {e}")
        await _send_slack_response(f"❌ Failed to get heat scores: {e}", user_id)
        return {"ok": False, "error": str(e)}


async def handle_cool_command(bookmaker: Optional[str], user_id: str) -> Dict:
    """
    Handle cooling command from Slack.

    Usage:
        arb cool fanduel   - Force FanDuel into 24h cooling period
    """
    if not bookmaker:
        await _send_slack_response(
            "❌ Usage: `arb cool <bookmaker>` (e.g., `arb cool fanduel`)",
            user_id,
        )
        return {"ok": False, "error": "Bookmaker required"}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{DECISION_GATEWAY_URL}/cool",
                json={"bookmaker": bookmaker, "hours": 24},
            )
            response.raise_for_status()
            data = response.json()

            message = (
                f"🧊 *Cooling Started for {bookmaker.upper()}*\n\n"
                f"*Duration:* {data.get('hours', 24)} hours\n"
                f"*Until:* {data.get('cooling_until', 'Unknown')}\n\n"
                f"No arb bets will be recommended for this bookmaker during the cooling period."
            )
            await _send_slack_response(message, user_id)
            return {"ok": True, "cooling_started": True}

    except Exception as e:
        logger.error(f"Cool command failed: {e}")
        await _send_slack_response(f"❌ Failed to start cooling: {e}", user_id)
        return {"ok": False, "error": str(e)}


async def handle_alert_control_command(action: str, user_id: str) -> Dict:
    """Handle alert control commands from Slack: mute, unmute, alerts."""
    if action == "mute":
        _alert_state["enabled"] = False
        _alert_state["disabled_at"] = datetime.utcnow().isoformat()
        _alert_state["disabled_by"] = f"slack:{user_id}"
        _save_alert_state()
        msg = "🔇 *Alerts MUTED*\nNo arb/EV/middle alerts will be sent until you run `arb unmute`."
        logger.info(f"Alerts muted by Slack user {user_id}")
        await _send_slack_response(msg, user_id)
        return {"ok": True, "enabled": False}

    elif action == "unmute":
        _alert_state["enabled"] = True
        _alert_state["disabled_at"] = None
        _alert_state["disabled_by"] = None
        _save_alert_state()
        msg = "🔔 *Alerts UNMUTED*\nArb/EV/middle alerts are active again."
        logger.info(f"Alerts unmuted by Slack user {user_id}")
        await _send_slack_response(msg, user_id)
        return {"ok": True, "enabled": True}

    elif action == "alerts":
        stats = _alert_stats
        state = "🔔 ON" if _alert_state["enabled"] else "🔇 MUTED"
        lines = [
            f"📊 *Alert Control Status: {state}*",
            "",
            f"*Received:* {stats['total_received']}",
            f"*Sent:* {stats['total_sent']}",
            f"*Suppressed (muted):* {stats['suppressed_muted']}",
            f"*Suppressed (dedupe):* {stats['suppressed_dedupe']}",
            f"*Suppressed (quality):* {stats['suppressed_quality']}",
            f"*Suppressed (rate limit):* {stats['suppressed_rate_limit']}",
            "",
            f"*Cooldown:* {ALERT_COOLDOWN_SECONDS}s",
            f"*Rate limit:* {ALERT_RATE_LIMIT_PER_MINUTE}/min",
            f"*Min arb profit:* {MIN_ARB_PROFIT_PCT}%",
            f"*Min EV:* {MIN_EV_PCT}%",
            f"*Min middle gap:* {MIN_MIDDLE_GAP}",
            "",
            f"*Dedupe entries:* {len(_alert_dedupe)}",
            f"*Lifecycle entries:* {len(_alert_lifecycle)}",
            f"*Pending alerts:* {len(_pending_alerts)}",
        ]
        if _alert_state.get("disabled_at"):
            lines.append(f"*Muted at:* {_alert_state['disabled_at']}")
            lines.append(f"*Muted by:* {_alert_state['disabled_by']}")
        msg = "\n".join(lines)
        await _send_slack_response(msg, user_id)
        return {"ok": True, "stats": dict(stats)}

    return {"ok": False, "error": f"Unknown alert action: {action}"}


async def _send_slack_response(message: str, user_id: str) -> None:
    """Send a response message to Slack."""
    if SLACK_BOT_TOKEN:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                json={
                    "channel": DEFAULT_CHANNEL or user_id,
                    "text": message,
                    "mrkdwn": True,
                },
            )


# ─────────────────────────────────────────────────────────────────────────────
# Slack-Based 2FA Commands
# ─────────────────────────────────────────────────────────────────────────────


async def handle_2fa_submission(request_id_prefix: str, code: str, user_id: str) -> Dict:
    """
    Handle 2FA code submission from Slack.

    User types: 2fa <short_id> <code>
    We forward the code to market_feed which enters it into the browser.
    """
    try:
        # First, find the full request ID by checking pending requests
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get pending requests to find the full ID
            pending_response = await client.get(f"{MARKET_FEED_URL}/2fa/pending")
            pending_data = pending_response.json()

            # Find matching request
            full_request_id = None
            bookmaker = None
            for req in pending_data.get("pending", []):
                if (req["request_id"].startswith(request_id_prefix) or
                    req["short_id"] == request_id_prefix):
                    full_request_id = req["request_id"]
                    bookmaker = req["bookmaker"]
                    break

            if not full_request_id:
                await _send_slack_response(
                    f"❌ 2FA request `{request_id_prefix}` not found or expired.",
                    user_id
                )
                return {"success": False, "error": "Request not found"}

            # Submit the code
            submit_response = await client.post(
                f"{MARKET_FEED_URL}/2fa/submit",
                json={
                    "request_id": full_request_id,
                    "code": code,
                    "submitted_by": user_id,
                },
            )
            submit_data = submit_response.json()

            if submit_data.get("success"):
                await _send_slack_response(
                    f"✅ 2FA code submitted for *{bookmaker}*",
                    user_id
                )
                return {"success": True, "bookmaker": bookmaker}
            else:
                error = submit_data.get("error", "Unknown error")
                await _send_slack_response(
                    f"❌ Failed to submit 2FA code: {error}",
                    user_id
                )
                return {"success": False, "error": error}

    except Exception as e:
        logger.error(f"2FA submission failed: {e}")
        await _send_slack_response(f"❌ 2FA submission failed: {e}", user_id)
        return {"success": False, "error": str(e)}


async def handle_login_command(bookmaker: Optional[str], user_id: str) -> Dict:
    """
    Handle login command from Slack.

    Usage:
        arb login                   - Start login for all configured bookmakers
        arb login fanduel           - Start login for specific bookmaker
        arb login visual fanduel    - Open visible browser window for manual login
    """
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Check for visual login mode: "arb login visual <bookmaker>"
            if bookmaker and bookmaker.lower() == "visual":
                await _send_slack_response(
                    "❌ Usage: `arb login visual <bookmaker>` (e.g., `arb login visual fanduel`)",
                    user_id
                )
                return {"success": False, "error": "Missing bookmaker for visual login"}

            if bookmaker:
                # Check if it's a visual login request passed as second arg
                # This handles the case where the regex captures "visual" as bookmaker
                # and the actual bookmaker comes later in the message

                # For now, handle standard bookmaker login
                # Login to specific bookmaker
                await _send_slack_response(
                    f"🔐 Starting login for *{bookmaker}*...",
                    user_id
                )

                response = await client.post(
                    f"{MARKET_FEED_URL}/feeds/control",
                    json={"bookmaker": bookmaker, "action": "start"},
                )
                data = response.json()

                if data.get("success"):
                    await _send_slack_response(
                        f"✅ *{bookmaker}* login initiated",
                        user_id
                    )
                else:
                    await _send_slack_response(
                        f"❌ *{bookmaker}* login failed: {data.get('message', 'Unknown error')}",
                        user_id
                    )

                return data
            else:
                # Login to all configured bookmakers
                feeds_response = await client.get(f"{MARKET_FEED_URL}/feeds")
                feeds_data = feeds_response.json()

                feeds = feeds_data.get("feeds", [])
                if not feeds:
                    await _send_slack_response(
                        "❌ No feeds configured. Check FEED_CONFIGS environment variable.",
                        user_id
                    )
                    return {"success": False, "error": "No feeds configured"}

                await _send_slack_response(
                    f"🔐 Starting login for {len(feeds)} bookmaker(s)...",
                    user_id
                )

                results = []
                for feed in feeds:
                    bm = feed.get("bookmaker")
                    if not bm:
                        continue

                    await _send_slack_response(
                        f"🔐 Logging into *{bm}*...",
                        user_id
                    )

                    try:
                        response = await client.post(
                            f"{MARKET_FEED_URL}/feeds/control",
                            json={"bookmaker": bm, "action": "start"},
                        )
                        data = response.json()
                        results.append({"bookmaker": bm, **data})
                    except Exception as e:
                        results.append({"bookmaker": bm, "success": False, "error": str(e)})

                # Summary
                success_count = sum(1 for r in results if r.get("success"))
                total_count = len(results)

                if success_count == total_count:
                    await _send_slack_response(
                        f"✅ All {total_count} bookmaker(s) login initiated",
                        user_id
                    )
                else:
                    await _send_slack_response(
                        f"⚠️ {success_count}/{total_count} bookmaker(s) login initiated",
                        user_id
                    )

                return {"success": success_count > 0, "results": results}

    except Exception as e:
        logger.error(f"Login command failed: {e}")
        await _send_slack_response(f"❌ Login failed: {e}", user_id)
        return {"success": False, "error": str(e)}


async def handle_visual_login_command(bookmaker: str, user_id: str) -> Dict:
    """
    Handle visual login command from Slack.

    Opens a visible browser window on the host machine for manual login.
    The user completes login (including 2FA), and the system saves the session.

    Usage:
        arb login visual fanduel
        arb login visual draftkings
        arb login visual fanatics
    """
    try:
        # Validate bookmaker
        if bookmaker.lower() not in ["fanduel", "draftkings", "fanatics"]:
            await _send_slack_response(
                f"❌ Visual login only supports: fanduel, draftkings, fanatics",
                user_id
            )
            return {"success": False, "error": "Invalid bookmaker"}

        await _send_slack_response(
            f"🖥️ Opening browser window for *{bookmaker}* login...\n"
            f"Complete login manually (including 2FA). The system will save your session.",
            user_id
        )

        # Increased timeout for visual login - it takes time to complete 2FA
        async with httpx.AsyncClient(timeout=600.0) as client:
            response = await client.post(
                f"{MARKET_FEED_URL}/login/visual/{bookmaker}",
                params={"timeout_seconds": 300},  # 5 minute timeout
            )
            data = response.json()

            if data.get("success"):
                await _send_slack_response(
                    f"✅ *{bookmaker}* login successful!\n"
                    f"Session saved. Future scraping will use this session.",
                    user_id
                )
            else:
                error = data.get("error", "Unknown error")
                await _send_slack_response(
                    f"❌ *{bookmaker}* visual login failed: {error}",
                    user_id
                )

            return data

    except Exception as e:
        logger.error(f"Visual login command failed: {e}")
        await _send_slack_response(f"❌ Visual login failed: {e}", user_id)
        return {"success": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# Slack Socket Mode Listener
# ─────────────────────────────────────────────────────────────────────────────


def _process_message_sync(text: str, user_id: str, channel: str) -> None:
    """Process a Slack message synchronously (runs in Socket Mode thread)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_process_message_async(text, user_id, channel))
    finally:
        loop.close()


async def _process_message_async(text: str, user_id: str, channel: str) -> None:
    """Process a Slack message - same logic as handle_slack_events."""
    # Parse 2FA code submission: "2fa <request_id> <code>"
    twofa_match = re.match(r"2fa\s+(\S+)\s+(\d{4,8})", text, re.IGNORECASE)
    if twofa_match:
        request_id_prefix = twofa_match.group(1)
        code = twofa_match.group(2)
        result = await handle_2fa_submission(request_id_prefix, code, user_id)
        logger.info(f"2FA submission result: {result}")
        return

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
        return

    # Parse visual login command: "arb login visual <bookmaker>"
    visual_login_match = re.match(
        r"arb\s+login\s+visual\s+(\S+)",
        text,
        re.IGNORECASE,
    )
    if visual_login_match:
        bookmaker = visual_login_match.group(1)
        result = await handle_visual_login_command(bookmaker, user_id)
        logger.info(f"Socket Mode visual login result: {result}")
        return

    # Parse service control commands
    arb_match = re.match(
        r"arb\s+(start|stop|restart|status|scrape|logs|heat|cool|login|mute|unmute|alerts)(?:\s+(\S+))?",
        text,
        re.IGNORECASE,
    )
    if arb_match:
        action = arb_match.group(1).lower()
        arg = arb_match.group(2)

        if action == "logs":
            result = await handle_logs_command(arg, user_id)
        elif action == "heat":
            result = await handle_heat_command(arg, user_id)
        elif action == "cool":
            result = await handle_cool_command(arg, user_id)
        elif action == "login":
            result = await handle_login_command(arg, user_id)
        elif action in ("mute", "unmute", "alerts"):
            result = await handle_alert_control_command(action, user_id)
        else:
            result = await handle_service_control(action, arg, user_id)
        logger.info(f"Socket Mode command result: {result}")
        return

    # Unknown command - send help
    if text.lower().startswith("arb") or text.lower().startswith("bet") or text.lower().startswith("2fa"):
        await _send_slack_response(
            "❓ Unknown command. Available commands:\n"
            "• `arb status` - Service status\n"
            "• `arb mute` - Mute arb alerts\n"
            "• `arb unmute` - Unmute arb alerts\n"
            "• `arb alerts` - Alert stats/status\n"
            "• `arb login [bookmaker]` - Login to bookmakers\n"
            "• `arb login visual <bookmaker>` - Open browser window for manual login\n"
            "• `arb scrape` - Trigger scrape\n"
            "• `arb logs [errors|browser|summary]` - View logs\n"
            "• `arb heat [bookmaker]` - View heat scores\n"
            "• `arb cool <bookmaker>` - Force cooling\n"
            "• `arb start|stop|restart <service>` - Control services\n"
            "• `bet <alert_id> <amount>` - Place a bet\n"
            "• `2fa <id> <code>` - Submit 2FA code",
            user_id,
        )


def _start_socket_mode() -> None:
    """Start Slack Socket Mode listener in a background thread."""
    if not SLACK_APP_TOKEN or not SLACK_BOT_TOKEN:
        logger.warning(
            "Socket Mode disabled: SLACK_APP_TOKEN or SLACK_BOT_TOKEN not set. "
            "Bot can send messages but cannot receive commands from Slack."
        )
        return

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        bolt_app = App(token=SLACK_BOT_TOKEN)

        @bolt_app.event("message")
        def handle_message_event(event, say):
            """Handle all message events from Slack."""
            # Ignore bot messages
            if event.get("bot_id") or event.get("subtype"):
                return

            text = event.get("text", "").strip()
            user_id = event.get("user", "")
            channel = event.get("channel", "")

            if not text:
                return

            logger.info(f"Socket Mode received: '{text}' from user {user_id}")
            _process_message_sync(text, user_id, channel)

        handler = SocketModeHandler(bolt_app, SLACK_APP_TOKEN)
        logger.info("🔌 Starting Slack Socket Mode listener...")
        handler.start()  # This blocks, so it must run in a thread

    except Exception as e:
        logger.error(f"Failed to start Socket Mode: {e}")


@app.on_event("startup")
def startup_socket_mode():
    """Start Socket Mode listener when FastAPI starts."""
    _load_alert_state()
    thread = threading.Thread(target=_start_socket_mode, daemon=True)
    thread.start()
    logger.info("Socket Mode thread launched")