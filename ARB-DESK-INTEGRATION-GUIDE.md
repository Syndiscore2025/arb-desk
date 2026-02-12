# ArbDesk Integration Guide
## External Monitoring, Alerting & Monetization Workflows

**Document Purpose:** Technical reference for integrating external systems with ArbDesk's arbitrage detection pipeline. No refactoring or feature additions—documentation of current capabilities only.

---

## 1. Data Sources

### 1.1 Odds Ingestion Methods

| Method | Implementation | Update Frequency | Rate Limits | Latency |
|--------|---------------|------------------|-------------|---------|
| **The Odds API** | `services/market_feed/app/adapters/odds_api_adapter.py` | 5-30s delay on live odds | 500 req/month (free), 10k-50k (paid) | ~1-2s API response |
| **API Interception** | `services/market_feed/app/adapters/intercepting_adapter.py` | Real-time (3-10s polling) | None (uses login session) | <1s (direct from browser) |
| **Browser Scraping** | `services/market_feed/app/adapters/playwright_generic.py` | Configurable (5-60s) | Sportsbook-dependent | 2-5s per page |
| **Manual Entry** | Not implemented | N/A | N/A | N/A |

**Current Production Mode:** Hybrid
- Pre-game odds: The Odds API (reliable, no login)
- Live/in-play: API interception (real-time, requires login)

### 1.2 Supported Sportsbooks
- **Connecticut Legal:** FanDuel, DraftKings, Fanatics
- **Offshore:** Pinnacle (optional, for CLV tracking)
- **Prediction Markets:** Polymarket, Kalshi (optional)

### 1.3 Dependencies
- **The Odds API:** Requires `ODDS_API_KEY` environment variable
- **Browser-based:** Requires valid login sessions (cookies or visual login)
- **Proxies:** Optional but recommended (`PROXY_LIST` env var)

---

## 2. Opportunity Detection Pipeline

### 2.1 Pipeline Flow

```
market_feed (scrape) → odds_ingest (/process) → arb_math (/arbitrage) → decision_gateway (/decision) → slack_notifier (/notify)
```

### 2.2 Arbitrage Calculation Logic

**Location:** `services/arb_math/app/main.py`

**Core Algorithm:**
```python
# Lines 42-46
def _calculate_profit_percentage(implied_sum: float) -> float:
    if implied_sum >= 1.0:
        return 0.0
    return ((1.0 / implied_sum) - 1.0) * 100
```

**Implied Probability Sum:**
```python
# Line 113-115
implied_sum = sum(1.0 / odds for odds, _ in best_by_selection.values())
has_arb = implied_sum < 1.0
```

### 2.3 Stake Allocation Logic

**Location:** `services/arb_math/app/main.py` lines 49-78

**Formula:**
```python
stake = (total_stake / implied_sum) / odds
payout = stake * odds
```

**Default Total Stake:** $1000 (configurable via API parameter)

### 2.4 Threshold Configuration

**Location:** `services/arb_math/app/main.py` lines 13-16

```python
TIER_FIRE = 3.0       # 🔥 >3% profit
TIER_LIGHTNING = 1.5  # ⚡ 1.5-3% profit
# Below 1.5% = ℹ️ info tier
```

**Live Boost:** Live arbs are promoted one tier (e.g., 2% live → 🔥 instead of ⚡)

**Minimum Profit Filter:** Optional `min_profit_pct` parameter on `/arbitrage` endpoint

---

## 3. Output & Storage

### 3.1 Storage Mechanism

**Current Implementation:** **In-memory only** (no persistent database storage for opportunities)

| Data Type | Storage | Location | Persistence |
|-----------|---------|----------|-------------|
| Opportunities | In-memory dict | `services/slack_notifier/app/main.py` line 66 | Until process restart |
| Heat Scores | In-memory dict | `services/decision_gateway/app/stealth_advisor.py` line 171 | Until process restart |
| Odds Data | Not stored | N/A | Transient |
| PostgreSQL | Available | Port 5432 | **Not currently used** |

**Critical Note:** PostgreSQL is running but **not integrated**. All opportunity data is ephemeral.

### 3.2 Opportunity Schema

**Location:** `shared/schemas.py` lines 41-52

```python
class ArbOpportunity(BaseModel):
    event_id: str
    market: str
    implied_prob_sum: float
    has_arb: bool
    notes: Optional[str] = None
    profit_percentage: Optional[float] = None  # e.g., 2.5 for 2.5% profit
    legs: List[Dict[str, Any]] = []  # Each leg with bookmaker, selection, odds, stake
    is_live: bool = False
    detected_at: datetime
    expires_estimate_seconds: Optional[int] = None
```

**Leg Structure** (from `services/arb_math/app/main.py` lines 67-76):
```python
{
    "bookmaker": str,
    "selection": str,
    "odds_decimal": float,
    "stake": float,
    "payout": float,
    "sport": str,
    "market": str,
    "event_id": str
}
```

---

## 4. Integration Points

### 4.1 Existing API Endpoints

| Service | Port | Endpoint | Method | Purpose |
|---------|------|----------|--------|---------|
| **arb_math** | 8002 | `/arbitrage` | POST | Calculate arb from odds array |
| **odds_ingest** | 8001 | `/process` | POST | Full pipeline (arb calc + decision + alert) |
| **decision_gateway** | 8004 | `/decision` | POST | AI/rule-based filtering |
| **decision_gateway** | 8004 | `/heat` | GET | Get all bookmaker heat scores |
| **slack_notifier** | 8005 | `/notify` | POST | Send Slack alert |
| **market_feed** | 8006 | `/scrape-all` | POST | Trigger scrape of all feeds |
| **market_feed** | 8006 | `/odds-api/odds` | GET | Fetch from The Odds API |
| **market_feed** | 8006 | `/live/scrape/{bookmaker}` | POST | Scrape live odds via interception |

### 4.2 Recommended Read-Only Endpoint (Does Not Exist)

**Safest Integration Point:** Add new endpoint to `odds_ingest` service

**Proposed Location:** `services/odds_ingest/app/main.py`

**Endpoint:** `GET /opportunities/recent`

**Implementation:**
```python
# Store last N opportunities in a deque
from collections import deque
_recent_opportunities = deque(maxlen=100)

@app.get("/opportunities/recent")
def get_recent_opportunities(limit: int = 20):
    return list(_recent_opportunities)[-limit:]
```

**Why This Location:**
- `odds_ingest` is the orchestrator—sees all opportunities
- Read-only, no side effects
- Minimal code change (10 lines)

### 4.3 Webhook Event Emission (Does Not Exist)

**Current State:** No webhook support

**Recommended Implementation:** Add webhook POST to `odds_ingest` after decision gateway approval

**Location:** `services/odds_ingest/app/main.py` lines 81-96 (after Slack notification)

**Pseudocode:**
```python
# After line 96 in odds_ingest/app/main.py
WEBHOOK_URL = os.getenv("ARB_WEBHOOK_URL")
if WEBHOOK_URL:
    try:
        webhook_payload = {
            "opportunity": opp,
            "decision": decision,
            "rationale": rationale,
            "timestamp": datetime.utcnow().isoformat()
        }
        httpx.post(WEBHOOK_URL, json=webhook_payload, timeout=5.0)
    except Exception:
        pass  # Don't block pipeline on webhook failure
```

---

## 5. Complete Data Schema (JSON)

### 5.1 Arbitrage Opportunity (Full Example)

```json
{
  "event_id": "lakers_vs_celtics_20260212",
  "market": "Moneyline",
  "sport": "nba",
  "implied_prob_sum": 0.953,
  "has_arb": true,
  "profit_percentage": 4.94,
  "is_live": true,
  "detected_at": "2026-02-12T19:45:23.123Z",
  "expires_estimate_seconds": 30,
  "notes": "🎯 4.94% arb (fire). Stakes for $1000.",
  "legs": [
    {
      "bookmaker": "fanduel",
      "selection": "Lakers",
      "odds_decimal": 2.20,
      "stake": 476.19,
      "payout": 1047.62,
      "sport": "nba",
      "market": "Moneyline",
      "event_id": "lakers_vs_celtics_20260212"
    },
    {
      "bookmaker": "draftkings",
      "selection": "Celtics",
      "odds_decimal": 2.10,
      "stake": 523.81,
      "payout": 1099.99,
      "sport": "nba",
      "market": "Moneyline",
      "event_id": "lakers_vs_celtics_20260212"
    }
  ]
}
```

### 5.2 MarketOdds (Input Schema)

```json
{
  "event_id": "lakers_vs_celtics_20260212",
  "sport": "nba",
  "market": "Moneyline",
  "bookmaker": "fanduel",
  "selection": "Lakers",
  "odds_decimal": 2.20,
  "captured_at": "2026-02-12T19:45:20.000Z",
  "market_type": "moneyline",
  "is_live": true,
  "is_boosted": false,
  "line": null,
  "player_name": null,
  "prop_type": null,
  "period": "full_game",
  "expires_at": null
}
```

---

## 6. Latency & Performance

### 6.1 Pipeline Timing

| Stage | Average Latency | Bottleneck |
|-------|----------------|------------|
| Odds scrape (API) | 1-2s | Network I/O |
| Odds scrape (browser) | 2-5s | Page load + rendering |
| Arb calculation | <10ms | CPU (negligible) |
| Decision gateway | 50-200ms | AI API call (if enabled) |
| Slack notification | 100-300ms | Slack API |
| **Total (API source)** | **1.5-3s** | Network-bound |
| **Total (browser source)** | **3-6s** | Browser automation |

### 6.2 Known Bottlenecks

1. **Browser Page Load:** 2-5s per sportsbook page (Playwright rendering)
2. **AI Decision Gateway:** 50-200ms if OpenAI enabled (can disable for speed)
3. **Sequential Processing:** Pipeline is synchronous (no parallel arb detection)

### 6.3 Optimization Opportunities

- **Parallel Scraping:** Run multiple bookmakers concurrently (not implemented)
- **Skip Decision Gateway:** Direct arb_math → slack_notifier (bypass AI filter)
- **Use The Odds API:** Faster than browser scraping for pre-game

---

## 7. Compliance & Safety

### 7.1 Sportsbook Terms of Service

**Potential Violations:**
- **Automated Betting:** All CT books prohibit bots (FanDuel, DraftKings, Fanatics ToS)
- **Multi-Accounting:** Prohibited (credential rotation feature violates this)
- **Arbitrage Detection:** Books actively limit/ban arb bettors

**Risk Levels:**
| Activity | Risk | Notes |
|----------|------|-------|
| **Alerting Only** | ✅ Low | Reading odds is legal |
| **Manual Bet Placement** | 🟡 Medium | Human places bets after alert |
| **Automated Execution** | 🔴 High | `/bet/place` endpoint violates ToS |

### 7.2 Safe vs. Risky Integration

**✅ SAFE:**
- Read-only API access (`/arbitrage`, `/opportunities/recent`)
- Webhook alerts to external system
- Human reviews and places bets manually
- Monitoring heat scores (`/heat`)

**🔴 RISKY:**
- Automated bet placement (`/bet/place`)
- High-frequency scraping (<5s intervals)
- Multi-account credential rotation
- Ignoring heat score warnings

### 7.3 Stealth Advisor Recommendations

**Location:** `services/decision_gateway/app/stealth_advisor.py`

**Decision Types:**
- `TAKE`: Safe to place bet
- `SKIP`: Pass on opportunity (heat too high)
- `COVER`: Place cover bet first (look recreational)
- `DELAY`: Wait before placing
- `COOL`: Account needs 24h cooling period

**Integration:** Always check `/decision` endpoint before automated execution

---

## 8. Minimal Integration Approach

### 8.1 Webhook Alert Setup (Recommended)

**Step 1:** Add environment variable
```bash
ARB_WEBHOOK_URL=https://your-system.com/api/arb-alerts
```

**Step 2:** Modify `services/odds_ingest/app/main.py` (after line 96)
```python
WEBHOOK_URL = os.getenv("ARB_WEBHOOK_URL")
if WEBHOOK_URL and actionable:
    for opp, decision, rationale in actionable:
        try:
            httpx.post(WEBHOOK_URL, json={
                "opportunity": opp,
                "decision": decision,
                "rationale": rationale,
                "timestamp": datetime.utcnow().isoformat()
            }, timeout=5.0)
        except:
            pass
```

**Step 3:** Restart `odds_ingest` service
```bash
docker compose restart odds_ingest
```

### 8.2 Polling Integration (Alternative)

**Not Recommended:** No `/opportunities/recent` endpoint exists

**Workaround:** Poll Slack API for messages in `#arb-alerts` channel

---

## 9. Key File Paths

| Component | Path |
|-----------|------|
| **Arb Calculation** | `services/arb_math/app/main.py` |
| **Pipeline Orchestrator** | `services/odds_ingest/app/main.py` |
| **Decision/Filtering** | `services/decision_gateway/app/main.py` |
| **Stealth Advisor** | `services/decision_gateway/app/stealth_advisor.py` |
| **Slack Alerts** | `services/slack_notifier/app/main.py` |
| **Schemas** | `shared/schemas.py` |
| **The Odds API Adapter** | `services/market_feed/app/adapters/odds_api_adapter.py` |
| **API Interception** | `services/market_feed/app/adapters/intercepting_adapter.py` |
| **Docker Compose** | `docker-compose.yml` |
| **Environment Config** | `.env.example` |

---

## 10. Summary

**Current State:**
- ✅ Arbitrage detection works (tested, 90 tests passing)
- ✅ Slack alerting works
- ✅ Dual odds sources (API + browser interception)
- ❌ No persistent storage (opportunities lost on restart)
- ❌ No webhook support (Slack-only alerts)
- ❌ No read-only API for external systems

**Minimal Integration (5 lines of code):**
1. Add `ARB_WEBHOOK_URL` to `.env`
2. Add webhook POST in `odds_ingest/app/main.py` after line 96
3. Restart service

**Safe Monetization:**
- Use webhook alerts → human reviews → manual bet placement
- Monitor `/heat` endpoint to avoid account bans
- Never automate bet execution (`/bet/place` violates ToS)

---

**Document Version:** 1.0  
**Last Updated:** 2026-02-12  
**System Version:** Commit 9f5e6f4

