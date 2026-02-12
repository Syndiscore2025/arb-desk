# ArbDesk Integration Summary

**Quick reference for external monitoring, alerting, and monetization integration.**

---

## Key File Paths

### Core Services
```
services/
├── market_feed/          # Odds scraping (browser + API)
│   ├── app/main.py       # FastAPI app, scraping endpoints
│   └── app/adapters/     # Odds sources (Playwright, The Odds API, etc.)
├── odds_ingest/          # Pipeline orchestrator
│   └── app/main.py       # Routes odds → arb_math → decision_gateway → slack
├── arb_math/             # Arbitrage calculator
│   └── app/main.py       # Core ROI/stake calculation logic
├── decision_gateway/     # AI filtering
│   ├── app/main.py       # Decision endpoint
│   └── app/stealth_advisor.py  # Heat tracking, skip logic
└── slack_notifier/       # Alert delivery
    └── app/main.py       # ⭐ WEBHOOK INSERTION POINT (line 223)

shared/
└── schemas.py            # Pydantic models (MarketOdds, ArbOpportunity)

docker-compose.yml        # Service ports and configuration
.env                      # Environment variables
```

---

## Data Flow

```
┌─────────────┐
│ market_feed │  Scrapes odds from sportsbooks
└──────┬──────┘
       │ POST /process
       ▼
┌─────────────┐
│ odds_ingest │  Orchestrates pipeline
└──────┬──────┘
       │ POST /arbitrage
       ▼
┌─────────────┐
│  arb_math   │  Calculates arbitrage (ROI, stakes)
└──────┬──────┘
       │ POST /decision
       ▼
┌─────────────┐
│  decision_  │  Filters with stealth advisor
│   gateway   │
└──────┬──────┘
       │ POST /alert/arb
       ▼
┌─────────────┐
│   slack_    │  Sends alerts (+ webhook emission point)
│  notifier   │
└─────────────┘
```

---

## Minimal Webhook Integration

### 1. Add Environment Variable

**File**: `.env`

```bash
# Add this line
WEBHOOK_URLS=["https://your-service.com/webhooks/arb","https://backup.com/webhooks/arb"]
WEBHOOK_SECRET=your-secret-key-here
```

### 2. Modify Slack Notifier

**File**: `services/slack_notifier/app/main.py`

**Location**: After line 223 (after `notify(notification)`)

**Code to Add**:
```python
# At top of file (with other imports)
import hmac
import hashlib

# After line 29 (with other env vars)
WEBHOOK_URLS = json.loads(os.getenv("WEBHOOK_URLS", "[]"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# After line 223 (inside send_arb_alert function)
# Emit webhooks
for webhook_url in WEBHOOK_URLS:
    try:
        payload = {
            "event": "arbitrage_detected",
            "timestamp": datetime.utcnow().isoformat(),
            "alert_id": alert.alert_id,
            "tier": tier,
            "opportunity": opportunity.model_dump(mode="json"),
            "deep_links": deep_links,
        }
        
        # Sign payload
        signature = ""
        if WEBHOOK_SECRET:
            body_bytes = json.dumps(payload).encode()
            signature = hmac.new(
                WEBHOOK_SECRET.encode(),
                body_bytes,
                hashlib.sha256
            ).hexdigest()
        
        # Send webhook
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                webhook_url,
                json=payload,
                headers={"X-Arb-Signature": signature} if signature else {}
            )
            logger.info(f"Webhook delivered to {webhook_url}")
    except Exception as e:
        logger.warning(f"Webhook delivery failed to {webhook_url}: {e}")
```

### 3. Restart Service

```bash
docker compose restart slack_notifier
```

---

## Webhook Payload Schema

```json
{
  "event": "arbitrage_detected",
  "timestamp": "2026-02-12T10:00:15Z",
  "alert_id": "a7b2c3d4-e5f6-g7h8-i9j0-k1l2m3n4o5p6",
  "tier": "fire",
  "opportunity": {
    "event_id": "lakers-celtics-2026-02-12",
    "market": "moneyline",
    "implied_prob_sum": 0.953,
    "has_arb": true,
    "profit_percentage": 4.94,
    "is_live": false,
    "detected_at": "2026-02-12T10:00:15Z",
    "expires_estimate_seconds": 300,
    "legs": [
      {
        "bookmaker": "fanduel",
        "selection": "Lakers",
        "odds_decimal": 2.15,
        "stake": 476.19,
        "payout": 1023.81,
        "sport": "nba",
        "market": "moneyline",
        "event_id": "lakers-celtics-2026-02-12"
      },
      {
        "bookmaker": "draftkings",
        "selection": "Celtics",
        "odds_decimal": 2.05,
        "stake": 523.81,
        "payout": 1073.81,
        "sport": "nba",
        "market": "moneyline",
        "event_id": "lakers-celtics-2026-02-12"
      }
    ]
  },
  "deep_links": {
    "fanduel": "https://sportsbook.fanduel.com/event/lakers-celtics",
    "draftkings": "https://sportsbook.draftkings.com/event/lakers-celtics"
  }
}
```

---

## API Endpoints (Read-Only Access)

### Get Arbitrage Opportunities

**Endpoint**: `POST http://localhost:8002/arbitrage`

**Request**:
```json
{
  "odds": [
    {
      "event_id": "lakers-celtics",
      "sport": "nba",
      "market": "moneyline",
      "bookmaker": "fanduel",
      "selection": "Lakers",
      "odds_decimal": 2.15,
      "captured_at": "2026-02-12T10:00:00Z"
    },
    {
      "event_id": "lakers-celtics",
      "sport": "nba",
      "market": "moneyline",
      "bookmaker": "draftkings",
      "selection": "Celtics",
      "odds_decimal": 2.05,
      "captured_at": "2026-02-12T10:00:00Z"
    }
  ]
}
```

**Response**:
```json
{
  "opportunities": [
    {
      "event_id": "lakers-celtics",
      "market": "moneyline",
      "implied_prob_sum": 0.953,
      "has_arb": true,
      "profit_percentage": 4.94,
      "legs": [...],
      "notes": "🎯 4.94% arb (fire). Stakes for $1000."
    }
  ],
  "evaluated_at": "2026-02-12T10:00:15Z"
}
```

### Trigger Scrape

**Endpoint**: `POST http://localhost:8006/scrape-all`

**Response**:
```json
{
  "results": [
    {
      "bookmaker": "fanduel",
      "success": true,
      "odds": [...],
      "scraped_at": "2026-02-12T10:00:15Z"
    }
  ]
}
```

### Get Heat Scores

**Endpoint**: `GET http://localhost:8004/heat`

**Response**:
```json
{
  "bookmakers": {
    "fanduel": {
      "heat_score": 45.2,
      "win_rate": 0.62,
      "total_bets": 28,
      "arb_bets_today": 3,
      "is_hot": false
    }
  },
  "timestamp": "2026-02-12T10:00:15Z"
}
```

---

## Performance Metrics

| Metric | Value |
|--------|-------|
| **Pre-game latency** | 2-8 seconds (odds update → detection) |
| **Live latency** | <1 second (API interception) or 3-15s (polling) |
| **Arb calculation** | <100ms |
| **Decision filtering** | 200-500ms (AI) or <50ms (rule-based) |
| **Webhook delivery** | <500ms (async, non-blocking) |

---

## Compliance & Safety

### ✅ Safe Activities
- Detecting arbitrage opportunities (read-only)
- Sending alerts/notifications
- Displaying opportunities in dashboard
- Providing API access to opportunity feed

### ❌ Risky Activities
- Automated bet placement (violates most sportsbook ToS)
- High-frequency scraping (<5 second intervals)
- Bypassing CAPTCHA/2FA without user interaction

### Built-In Stealth Features
- Heat score tracking (0-100 per bookmaker)
- Strategic skip probability (intentionally pass on some arbs)
- Stake reduction when heat is high (60-100% of original)
- Randomized delays (1-10 seconds)

---

## Testing

### 1. Start ArbDesk
```bash
docker compose up -d
```

### 2. Test Arbitrage Calculation
```bash
curl -X POST http://localhost:8002/arbitrage \
  -H "Content-Type: application/json" \
  -d @test_odds.json
```

### 3. Verify Webhook Delivery
- Check your webhook endpoint logs
- Verify signature validation
- Confirm payload structure

### 4. Monitor Logs
```bash
docker compose logs -f slack_notifier
```

---

## Quick Reference

| Need | Endpoint | Port |
|------|----------|------|
| Calculate arbitrage | `POST /arbitrage` | 8002 |
| Trigger scrape | `POST /scrape-all` | 8006 |
| Get heat scores | `GET /heat` | 8004 |
| Send alert | `POST /alert/arb` | 8005 |
| Health check | `GET /health` | All |

**OpenAPI Docs**: `http://localhost:{port}/docs`

---

## Next Steps

1. ✅ Review `INTEGRATION-GUIDE.md` for detailed documentation
2. ✅ Add `WEBHOOK_URLS` to `.env`
3. ✅ Implement webhook handler on your service
4. ✅ Modify `slack_notifier/app/main.py` (20 lines)
5. ✅ Test with sample odds
6. ✅ Deploy and monitor

**Estimated Implementation Time**: 1-2 hours

