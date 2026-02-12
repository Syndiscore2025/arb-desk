# ArbDesk Integration Guide

**Purpose**: Technical documentation for integrating external monitoring, alerting, and monetization workflows with the ArbDesk arbitrage detection system.

**Scope**: Read-only integration points only. No refactoring or feature additions.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Sources](#data-sources)
3. [Opportunity Detection Pipeline](#opportunity-detection-pipeline)
4. [Output & Storage](#output--storage)
5. [Integration Points](#integration-points)
6. [Data Schema](#data-schema)
7. [Latency & Performance](#latency--performance)
8. [Compliance & Safety](#compliance--safety)
9. [Recommended Integration Approach](#recommended-integration-approach)

---

## System Overview

ArbDesk is a microservices-based sports betting arbitrage detection system with 6 core services:

| Service | Port | Purpose |
|---------|------|---------|
| **market_feed** | 8006 | Scrapes odds from sportsbooks (browser automation + API) |
| **odds_ingest** | 8001 | Orchestrates the full pipeline (routing hub) |
| **arb_math** | 8002 | Calculates arbitrage opportunities and optimal stakes |
| **decision_gateway** | 8004 | AI-powered filtering (stealth advisor) |
| **slack_notifier** | 8005 | Sends alerts and handles commands |
| **postgres** | 5432 | Database (currently minimal usage) |

**Data Flow**:
```
market_feed → odds_ingest → arb_math → decision_gateway → slack_notifier
```

---

## Data Sources

### Odds Ingestion Methods

ArbDesk supports **hybrid ingestion**:

1. **Browser Automation (Playwright)**
   - **Location**: `services/market_feed/app/adapters/playwright_*.py`
   - **Method**: Stealth browser scraping with anti-detection
   - **Requires**: Login credentials, 2FA support (TOTP or Slack-based)
   - **Sportsbooks**: FanDuel, DraftKings, Fanatics (Connecticut legal books)
   - **Latency**: 2-5 seconds per scrape
   - **Rate Limits**: Configurable poll intervals (5-60 seconds)

2. **The Odds API (Third-Party)**
   - **Location**: `services/market_feed/app/adapters/odds_api_adapter.py`
   - **Method**: REST API calls to https://the-odds-api.com
   - **Requires**: API key (`ODDS_API_KEY` env var)
   - **Latency**: 1-2 seconds for pre-game, 5-30 seconds for live odds
   - **Rate Limits**: API plan-dependent (500-10,000 requests/month)
   - **Cost**: $79-199/month

3. **API Interception (Real-Time)**
   - **Location**: `services/market_feed/app/adapters/intercepting_adapter.py`
   - **Method**: Intercepts sportsbook internal API calls via Playwright
   - **Latency**: Real-time (no delay)
   - **Requires**: Login session

4. **Prediction Markets**
   - **Location**: `services/market_feed/app/adapters/prediction_markets.py`
   - **Platforms**: Polymarket, Kalshi
   - **Method**: API-based (no login required)

### Update Frequency

- **Pre-game odds**: 10-60 second intervals (configurable via `poll_interval_seconds`)
- **Live odds**: 3-15 second intervals (configurable via `live_poll_interval_seconds`)
- **Steam moves**: Detected via snapshot history (5-minute rolling window)

### Dependencies

- **Browser automation**: Requires Playwright, Chromium
- **The Odds API**: Requires API key and active subscription
- **Credentials**: Stored in `BOOKMAKER_CREDENTIALS` env var (JSON)

---

## Opportunity Detection Pipeline

### 1. Odds Collection
**Location**: `services/market_feed/app/main.py`

- Endpoint: `POST /scrape/{bookmaker}` or `POST /scrape-all`
- Returns: `ScrapeResult` with list of `MarketOdds`

### 2. Arbitrage Calculation
**Location**: `services/arb_math/app/main.py`

- Endpoint: `POST /arbitrage`
- Input: `ArbRequest` (list of `MarketOdds`)
- Logic:
  - Groups odds by `(event_id, market)`
  - Selects best odds for each selection
  - Calculates implied probability sum: `Σ(1/odds)`
  - **Arbitrage exists if**: `implied_sum < 1.0`
  - **Profit %**: `((1/implied_sum) - 1) * 100`

**ROI Calculation**:
```python
implied_sum = sum(1.0 / odds for odds in best_odds_per_selection)
profit_percentage = ((1.0 / implied_sum) - 1.0) * 100
```

**Stake Allocation** (Kelly-style proportional):
```python
stake_i = (total_stake / implied_sum) / odds_i
payout_i = stake_i * odds_i  # Equal payouts across all legs
```

### 3. Threshold Configuration

**Tiered Alerts** (hardcoded in `services/arb_math/app/main.py`):
- **Fire** 🔥: ≥ 3.0% profit
- **Lightning** ⚡: 1.5% - 3.0% profit
- **Info** ℹ️: < 1.5% profit

**Live Boost**: Live arbs are promoted one tier (e.g., 2% live → Fire instead of Lightning)

**Minimum Profit Filter**: Optional `min_profit_pct` parameter in `/arbitrage` endpoint

### 4. Stealth Filtering
**Location**: `services/decision_gateway/app/stealth_advisor.py`

- Endpoint: `POST /decision`
- Tracks per-bookmaker "heat scores" (0-100) based on:
  - Win rate (>72% triggers cooling)
  - Arb frequency (max 12/day per bookmaker)
  - Consecutive wins
- Decisions: `TAKE`, `SKIP`, `COVER`, `DELAY`, `COOL`
- Stake modifiers: 60-100% of original stake based on heat

---

## Output & Storage

### Current Storage

**In-Memory Only** (no persistent database for opportunities):
- Opportunities are calculated on-demand
- Alerts stored temporarily in `slack_notifier` service (`_pending_alerts` dict)
- Alert expiry: 5 minutes (configurable)

**Database Usage** (Postgres):
- Currently minimal (health checks only)
- Schema location: Not yet implemented for opportunities
- **Recommendation**: Opportunities are ephemeral by design (odds change rapidly)

### Programmatic Access

**Yes** - All opportunities are accessible via REST APIs:

1. **Direct Calculation**:
   ```bash
   POST http://localhost:8002/arbitrage
   Content-Type: application/json
   
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
       ...
     ]
   }
   ```

2. **Full Pipeline**:
   ```bash
   POST http://localhost:8001/process
   ```
   (Same payload as above, but includes decision gateway filtering)

---

## Integration Points

### Existing APIs

All services expose FastAPI endpoints (OpenAPI docs at `http://localhost:{port}/docs`):

#### Market Feed (Port 8006)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/feeds` | GET | List all configured feeds and status |
| `/scrape/{bookmaker}` | POST | Trigger scrape for specific bookmaker |
| `/scrape-all` | POST | Trigger scrape for all enabled feeds |
| `/bet/place` | POST | Place bet via browser automation |

#### Arb Math (Port 8002)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/arbitrage` | POST | Calculate arbitrage from odds array |

**Query Parameters**:
- `min_profit_pct` (float): Filter opportunities below this profit %
- `total_stake` (float): Total stake for calculating leg amounts (default: 1000)

#### Odds Ingest (Port 8001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/odds` | POST | Ingest odds (no processing) |
| `/process` | POST | Full pipeline: arb detection + filtering + alerts |

#### Decision Gateway (Port 8004)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/decision` | POST | Evaluate opportunity with stealth advisor |
| `/heat` | GET | Get all bookmaker heat scores |
| `/heat/{bookmaker}` | GET | Get specific bookmaker heat |
| `/record-bet` | POST | Record bet result for heat tracking |
| `/cool` | POST | Force cooling period on bookmaker |

#### Slack Notifier (Port 8005)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/notify` | POST | Send Slack notification |
| `/alert/arb` | POST | Send tiered arb alert |
| `/slack/events` | POST | Handle Slack events (bet commands) |

### Webhook Integration (Recommended Addition)

**Current State**: No webhook emission capability exists.

**Safest Integration Point**: `services/slack_notifier/app/main.py`

**Recommended Implementation**:
1. Add `WEBHOOK_URLS` environment variable (JSON array of URLs)
2. Modify `/alert/arb` endpoint to emit webhook after Slack notification
3. Payload format (see Data Schema section below)

**Example Addition** (pseudo-code):
```python
# In services/slack_notifier/app/main.py
WEBHOOK_URLS = json.loads(os.getenv("WEBHOOK_URLS", "[]"))

@app.post("/alert/arb")
async def send_arb_alert(opportunity: ArbOpportunity):
    # ... existing Slack logic ...

    # Emit webhook
    for webhook_url in WEBHOOK_URLS:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(webhook_url, json={
                    "event": "arbitrage_detected",
                    "timestamp": datetime.utcnow().isoformat(),
                    "opportunity": opportunity.model_dump(mode="json"),
                    "alert_id": alert.alert_id,
                    "tier": tier,
                })
        except Exception as e:
            logger.warning(f"Webhook delivery failed: {e}")
```

---

## Data Schema

### MarketOdds (Input)

**Location**: `shared/schemas.py`

```json
{
  "event_id": "lakers-celtics-2026-02-12",
  "sport": "nba",
  "market": "moneyline",
  "bookmaker": "fanduel",
  "selection": "Lakers",
  "odds_decimal": 2.15,
  "captured_at": "2026-02-12T10:00:00Z",
  "market_type": "moneyline",
  "is_live": false,
  "is_boosted": false,
  "original_odds": null,
  "line": null,
  "player_name": null,
  "prop_type": null,
  "period": null,
  "expires_at": null
}
```

**Field Descriptions**:
- `event_id`: Unique event identifier (format varies by adapter)
- `sport`: Sport code (nba, nfl, mlb, nhl, etc.)
- `market`: Market type (moneyline, spread, total, prop, future, etc.)
- `bookmaker`: Sportsbook identifier (fanduel, draftkings, fanatics, etc.)
- `selection`: Outcome name (team name, player name, over/under)
- `odds_decimal`: Decimal odds (e.g., 2.15 = +115 American)
- `market_type`: Enhanced market classification
- `is_live`: True if in-play/live odds
- `is_boosted`: True if promotional/boosted odds
- `line`: Spread/total line value (e.g., -3.5, 45.5)
- `player_name`: For player props
- `prop_type`: Prop category (points, rebounds, assists, etc.)

### ArbOpportunity (Output)

```json
{
  "event_id": "lakers-celtics-2026-02-12",
  "market": "moneyline",
  "implied_prob_sum": 0.953,
  "has_arb": true,
  "profit_percentage": 4.94,
  "is_live": false,
  "detected_at": "2026-02-12T10:00:15Z",
  "expires_estimate_seconds": 300,
  "notes": "🎯 4.94% arb (fire). Stakes for $1000.",
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
}
```

**Field Descriptions**:
- `implied_prob_sum`: Sum of implied probabilities (< 1.0 = arbitrage)
- `has_arb`: Boolean flag for arbitrage existence
- `profit_percentage`: Guaranteed profit % (e.g., 4.94 = 4.94%)
- `is_live`: True if live/in-play arbitrage
- `expires_estimate_seconds`: Estimated time before odds change (optional)
- `legs`: Array of bet placements with optimal stakes

**Guaranteed Profit Calculation**:
```
profit = payout - total_stake
profit_percentage = (profit / total_stake) * 100
```

### Recommended Webhook Payload

```json
{
  "event": "arbitrage_detected",
  "timestamp": "2026-02-12T10:00:15Z",
  "alert_id": "a7b2c3d4-e5f6-g7h8-i9j0-k1l2m3n4o5p6",
  "tier": "fire",
  "opportunity": {
    "event_id": "lakers-celtics-2026-02-12",
    "sport": "nba",
    "market": "moneyline",
    "profit_percentage": 4.94,
    "roi_decimal": 0.0494,
    "is_live": false,
    "legs": [
      {
        "bookmaker": "fanduel",
        "selection": "Lakers",
        "odds_decimal": 2.15,
        "odds_american": "+115",
        "stake_usd": 476.19,
        "payout_usd": 1023.81
      },
      {
        "bookmaker": "draftkings",
        "selection": "Celtics",
        "odds_decimal": 2.05,
        "odds_american": "+105",
        "stake_usd": 523.81,
        "payout_usd": 1073.81
      }
    ],
    "total_stake_usd": 1000.00,
    "guaranteed_profit_usd": 23.81,
    "detected_at": "2026-02-12T10:00:15Z",
    "expires_at": "2026-02-12T10:05:15Z"
  },
  "deep_links": {
    "fanduel": "https://sportsbook.fanduel.com/event/lakers-celtics",
    "draftkings": "https://sportsbook.draftkings.com/event/lakers-celtics"
  }
}
```

---

## Latency & Performance

### Average Latency (Odds Update → Opportunity Detection)

**Pre-Game Odds**:
- Browser scraping: 2-5 seconds
- The Odds API: 1-2 seconds
- Arb calculation: <100ms
- Decision filtering: 200-500ms (with AI), <50ms (rule-based)
- **Total**: 3-8 seconds (browser), 2-3 seconds (API)

**Live Odds**:
- API interception: Real-time (<500ms)
- The Odds API: 5-30 second delay
- Fast polling: 3-15 second intervals
- **Total**: 3-15 seconds (polling), <1 second (interception)

### Known Bottlenecks

1. **Browser Automation**:
   - Playwright page load: 1-3 seconds
   - DOM parsing: 500ms-2 seconds
   - Anti-detection delays: 1-5 seconds (randomized)
   - **Mitigation**: Use The Odds API or API interception for speed

2. **Network Latency**:
   - Sportsbook API response time: 200-1000ms
   - Docker internal networking: <10ms
   - **Mitigation**: Deploy closer to sportsbook servers (US East Coast)

3. **AI Decision Gateway** (optional):
   - Azure OpenAI API call: 200-500ms
   - **Mitigation**: Use rule-based filtering (set `AI_API_URL=""`)

4. **Rate Limiting**:
   - Browser scraping: Risk of IP ban if too frequent (<5 second intervals)
   - The Odds API: Plan-dependent (500-10,000 requests/month)
   - **Mitigation**: Residential proxies, credential rotation

### Performance Optimization

**Current Configuration**:
- Poll intervals: 10-60 seconds (pre-game), 3-15 seconds (live)
- Concurrent scraping: Disabled (sequential to avoid detection)
- Caching: None (odds change too rapidly)

**Recommendations**:
- Use The Odds API for pre-game odds (faster, more reliable)
- Use API interception for live odds (real-time)
- Deploy on DigitalOcean NYC region (closest to US sportsbooks)

---

## Compliance & Safety

### Sportsbook Terms of Service

**Automation Risks**:
- **Prohibited**: Most sportsbooks ban automated betting in ToS
- **Detection Methods**: Bot detection, behavioral analysis, IP tracking
- **Consequences**: Account limiting, suspension, fund seizure

**Safe Activities**:
- ✅ **Alerting**: Detecting opportunities and sending notifications (read-only)
- ✅ **Manual Execution**: Human places bets after reviewing alert
- ❌ **Automated Execution**: Bot places bets without human intervention

### Stealth Features (Built-In)

**Browser Automation**:
- Playwright stealth mode (anti-fingerprinting)
- Randomized delays (1-10 seconds)
- Human-like mouse movements
- Residential proxy support

**Betting Pattern Obfuscation**:
- Heat score tracking (0-100 per bookmaker)
- Strategic skip probability (intentionally pass on some arbs)
- Cover bet suggestions (small losing bets to appear recreational)
- Stake reduction when heat is high

**Configuration** (`services/decision_gateway/app/stealth_advisor.py`):
- `STEALTH_MAX_WIN_RATE`: 0.72 (72% win rate triggers cooling)
- `STEALTH_MAX_ARBS_PER_DAY`: 12 (max arb bets per bookmaker per day)
- `STEALTH_HEAT_DECAY_HOURS`: 18 (hours for heat to decay by half)

### Recommended Safe Integration

**For External Monitoring/Alerting**:
1. ✅ Poll `/arbitrage` endpoint for opportunities
2. ✅ Emit webhook events to external system
3. ✅ Display opportunities in dashboard/mobile app
4. ✅ Send push notifications to user
5. ❌ **DO NOT** auto-execute bets without explicit user confirmation

**For Monetization (SaaS)**:
1. ✅ Charge for access to opportunity feed
2. ✅ Provide API access to `/arbitrage` endpoint
3. ✅ Offer tiered plans (fire-only, all tiers, live odds)
4. ❌ **DO NOT** guarantee execution (odds change rapidly)
5. ❌ **DO NOT** claim "risk-free" (account limiting is a risk)

---

## Recommended Integration Approach

### Minimal Webhook Integration

**Goal**: Emit webhook events when new arbitrage opportunities are detected.

**Implementation Steps**:

1. **Add Environment Variable** (`.env`):
   ```bash
   WEBHOOK_URLS=["https://your-service.com/webhooks/arb"]
   ```

2. **Modify Slack Notifier** (`services/slack_notifier/app/main.py`):
   - Add webhook emission after line 223 (after Slack notification)
   - Use existing `httpx` client for HTTP POST
   - Include full `ArbOpportunity` payload + metadata

3. **Webhook Endpoint Requirements** (Your Service):
   - Accept POST requests with JSON payload
   - Respond with 200 OK within 5 seconds
   - Handle duplicate events (same `alert_id`)
   - Implement retry logic (ArbDesk does not retry)

4. **Security**:
   - Add `WEBHOOK_SECRET` env var for HMAC signature
   - Verify signature in your webhook handler
   - Use HTTPS only

**Example Webhook Handler** (Python/FastAPI):
```python
import hmac
import hashlib
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()
WEBHOOK_SECRET = "your-secret-key"

@app.post("/webhooks/arb")
async def handle_arb_webhook(request: Request):
    # Verify signature
    signature = request.headers.get("X-Arb-Signature")
    body = await request.body()
    expected = hmac.new(
        WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Process opportunity
    data = await request.json()
    opportunity = data["opportunity"]

    # Your logic here (store, alert, analyze, etc.)
    print(f"New arb: {opportunity['profit_percentage']}%")

    return {"status": "received"}
```

### Key Files for Integration

| File | Purpose |
|------|---------|
| `shared/schemas.py` | Data models (MarketOdds, ArbOpportunity) |
| `services/arb_math/app/main.py` | Arbitrage calculation logic |
| `services/odds_ingest/app/main.py` | Pipeline orchestration |
| `services/slack_notifier/app/main.py` | Alert delivery (webhook insertion point) |
| `services/decision_gateway/app/stealth_advisor.py` | Filtering logic |
| `docker-compose.yml` | Service configuration |

### Testing Integration

1. **Start ArbDesk**:
   ```bash
   docker compose up -d
   ```

2. **Send Test Odds**:
   ```bash
   curl -X POST http://localhost:8002/arbitrage \
     -H "Content-Type: application/json" \
     -d '{
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
     }'
   ```

3. **Verify Response**:
   - Should return `ArbOpportunity` with `has_arb: true`
   - Profit percentage should be ~4.94%

4. **Check Webhook Delivery** (after implementation):
   - Monitor your webhook endpoint logs
   - Verify payload structure matches schema

---

## Summary

**Current Capabilities**:
- ✅ Hybrid odds ingestion (browser + API)
- ✅ Real-time arbitrage detection
- ✅ Optimal stake calculation
- ✅ Tiered alert system
- ✅ Stealth filtering (heat tracking)
- ✅ REST API access to all data
- ❌ No webhook emission (requires minor addition)
- ❌ No persistent opportunity storage (by design)

**Safest Integration Point**:
- `services/slack_notifier/app/main.py` → `/alert/arb` endpoint
- Add webhook emission after Slack notification
- Minimal code change (~20 lines)

**Recommended Use Cases**:
- ✅ External monitoring dashboard
- ✅ Mobile app push notifications
- ✅ SaaS opportunity feed
- ✅ Analytics/backtesting
- ❌ Fully automated betting (ToS violation risk)

**Performance**:
- Pre-game: 2-8 seconds (odds update → detection)
- Live: <1 second (API interception) or 3-15 seconds (polling)
- Bottleneck: Browser automation (use The Odds API for speed)

**Compliance**:
- Alerting is safe (read-only)
- Automated execution violates most sportsbook ToS
- Built-in stealth features for manual execution

---

## Contact & Support

For questions about integration:
1. Review OpenAPI docs: `http://localhost:{port}/docs`
2. Check logs: `docker compose logs -f`
3. Test endpoints with Postman/curl
4. Review test suite: `tests/test_integration.py`

**Key Environment Variables**:
- `FEED_CONFIGS`: Sportsbook scraping configuration
- `BOOKMAKER_CREDENTIALS`: Login credentials (JSON)
- `ODDS_API_KEY`: The Odds API key (optional)
- `SLACK_WEBHOOK_URL`: Slack webhook for alerts
- `AI_API_URL`: Azure OpenAI endpoint (optional)
- `WEBHOOK_URLS`: External webhook URLs (to be added)


