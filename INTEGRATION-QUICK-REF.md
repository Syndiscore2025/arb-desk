# ArbDesk Integration Quick Reference

**One-page reference for external integration.**

---

## 🎯 Integration Goal

Add webhook emission to ArbDesk for external monitoring/alerting/monetization workflows.

---

## 📍 Webhook Insertion Point

**File**: `services/slack_notifier/app/main.py`  
**Function**: `send_arb_alert()` (line 175)  
**Location**: After line 223 (after `notify(notification)`)  
**Lines to Add**: ~20 lines

---

## 🔧 Implementation (3 Steps)

### 1. Environment Variables

Add to `.env`:
```bash
WEBHOOK_URLS=["https://your-service.com/webhooks/arb"]
WEBHOOK_SECRET=your-secret-key-here
```

### 2. Code Addition

```python
# At top of services/slack_notifier/app/main.py
import hmac
import hashlib

WEBHOOK_URLS = json.loads(os.getenv("WEBHOOK_URLS", "[]"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# After line 223 in send_arb_alert()
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
        
        signature = ""
        if WEBHOOK_SECRET:
            body_bytes = json.dumps(payload).encode()
            signature = hmac.new(
                WEBHOOK_SECRET.encode(),
                body_bytes,
                hashlib.sha256
            ).hexdigest()
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                webhook_url,
                json=payload,
                headers={"X-Arb-Signature": signature} if signature else {}
            )
    except Exception as e:
        logger.warning(f"Webhook failed: {e}")
```

### 3. Restart

```bash
docker compose restart slack_notifier
```

---

## 📊 Webhook Payload

```json
{
  "event": "arbitrage_detected",
  "timestamp": "2026-02-12T10:00:15Z",
  "alert_id": "a7b2c3d4-...",
  "tier": "fire",
  "opportunity": {
    "event_id": "lakers-celtics-2026-02-12",
    "market": "moneyline",
    "profit_percentage": 4.94,
    "is_live": false,
    "legs": [
      {
        "bookmaker": "fanduel",
        "selection": "Lakers",
        "odds_decimal": 2.15,
        "stake": 476.19,
        "payout": 1023.81
      },
      {
        "bookmaker": "draftkings",
        "selection": "Celtics",
        "odds_decimal": 2.05,
        "stake": 523.81,
        "payout": 1073.81
      }
    ]
  }
}
```

---

## 🔌 REST API Endpoints

### Calculate Arbitrage
```bash
POST http://localhost:8002/arbitrage
Content-Type: application/json

{
  "odds": [
    {
      "event_id": "test-event",
      "sport": "nba",
      "market": "moneyline",
      "bookmaker": "fanduel",
      "selection": "Team A",
      "odds_decimal": 2.15,
      "captured_at": "2026-02-12T10:00:00Z"
    },
    {
      "event_id": "test-event",
      "sport": "nba",
      "market": "moneyline",
      "bookmaker": "draftkings",
      "selection": "Team B",
      "odds_decimal": 2.05,
      "captured_at": "2026-02-12T10:00:00Z"
    }
  ]
}
```

### Trigger Scrape
```bash
POST http://localhost:8006/scrape-all
```

### Get Heat Scores
```bash
GET http://localhost:8004/heat
```

---

## ⚡ Performance

| Metric | Value |
|--------|-------|
| Pre-game latency | 2-8 seconds |
| Live latency | <1 second (interception) |
| Arb calculation | <100ms |
| Webhook delivery | <500ms (async) |

---

## 🛡️ Compliance

### ✅ Safe
- Detecting opportunities (read-only)
- Sending alerts/webhooks
- API access to opportunity feed
- Manual bet execution

### ❌ Risky
- Automated bet placement (ToS violation)
- High-frequency scraping (<5s intervals)

---

## 📁 Key Files

| File | Purpose |
|------|---------|
| `shared/schemas.py` | Data models |
| `services/arb_math/app/main.py` | ROI calculation |
| `services/slack_notifier/app/main.py` | **Webhook insertion point** |
| `services/decision_gateway/app/stealth_advisor.py` | Filtering logic |
| `docker-compose.yml` | Service ports |

---

## 🧪 Testing

```bash
# 1. Start ArbDesk
docker compose up -d

# 2. Test arbitrage calculation
curl -X POST http://localhost:8002/arbitrage \
  -H "Content-Type: application/json" \
  -d @test_odds.json

# 3. Monitor webhook delivery
docker compose logs -f slack_notifier

# 4. Check your webhook endpoint logs
```

---

## 📚 Full Documentation

- **Detailed Guide**: `INTEGRATION-GUIDE.md` (698 lines)
- **Summary**: `INTEGRATION-SUMMARY.md` (350 lines)
- **This Reference**: `INTEGRATION-QUICK-REF.md` (you are here)

---

## 🚀 Estimated Time

**Implementation**: 1-2 hours  
**Testing**: 30 minutes  
**Total**: 2-3 hours

---

## 📞 Support

- OpenAPI docs: `http://localhost:{port}/docs`
- Logs: `docker compose logs -f`
- Tests: `tests/test_integration.py`

