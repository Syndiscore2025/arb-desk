# ArbDesk Operations Guide

## System Overview

ArbDesk is an automated sports betting arbitrage detection system that:
- Polls FanDuel and DraftKings on randomized intervals (45-90 seconds)
- Detects arbitrage opportunities across both sportsbooks
- Sends real-time alerts to Slack when profitable arbs are found

---

## Quick Start

### 1. Start the System

```powershell
docker compose up -d
```

### 2. Import Your Login Cookies

Since browser automation doesn't work in Docker on Windows, you must import cookies from your browser.

#### Step A: Install Cookie-Editor Extension
- Chrome: https://cookie-editor.com/
- Firefox: https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/

#### Step B: Export Cookies from FanDuel
1. Open Chrome → Go to **fanduel.com**
2. Log in with your credentials (complete 2FA if prompted)
3. Click the **Cookie-Editor** extension icon
4. Click **Export** → **Export as JSON**
5. Copy the JSON to clipboard

#### Step C: Import FanDuel Cookies
```powershell
$cookies = @'
[PASTE YOUR FANDUEL COOKIES JSON HERE]
'@

Invoke-RestMethod -Uri "http://localhost:8010/cookies/import/fanduel" -Method POST -ContentType "application/json" -Body $cookies
```

#### Step D: Repeat for DraftKings
1. Go to **draftkings.com** → Log in
2. Export cookies with Cookie-Editor
3. Import:
```powershell
$cookies = @'
[PASTE YOUR DRAFTKINGS COOKIES JSON HERE]
'@

Invoke-RestMethod -Uri "http://localhost:8010/cookies/import/draftkings" -Method POST -ContentType "application/json" -Body $cookies
```

### 3. Verify Cookie Import
```powershell
Invoke-RestMethod -Uri "http://localhost:8010/cookies/status"
```

Expected output:
```json
{
  "fanduel": { "has_cookies": true, "cookie_count": 25 },
  "draftkings": { "has_cookies": true, "cookie_count": 30 }
}
```

---

## Slack Commands

Use these commands in your Slack channel where the bot is invited:

| Command | Description |
|---------|-------------|
| `arb status` | Check health of all services |
| `arb scrape` | Manually trigger a scrape of all sportsbooks |
| `arb scrape fanduel` | Scrape only FanDuel |
| `arb scrape draftkings` | Scrape only DraftKings |
| `arb help` | Show available commands |

### Example Usage
```
arb status
```
Response:
```
📊 Service Status:
✅ market_feed: healthy
✅ odds_ingest: healthy
✅ arb_math: healthy
✅ decision_gateway: healthy
✅ slack_notifier: healthy
```

---

## API Endpoints

### Health & Status
| Endpoint | Method | Description |
|----------|--------|-------------|
| `http://localhost:8010/health` | GET | Market feed health |
| `http://localhost:8010/feeds` | GET | List all feeds and status |
| `http://localhost:8010/polling/status` | GET | Auto-poller status |
| `http://localhost:8010/cookies/status` | GET | Cookie import status |

### Manual Controls
| Endpoint | Method | Description |
|----------|--------|-------------|
| `http://localhost:8010/scrape/{bookmaker}` | POST | Trigger manual scrape |
| `http://localhost:8010/polling/stop/{bookmaker}` | POST | Stop auto-polling |
| `http://localhost:8010/polling/start/{bookmaker}` | POST | Start auto-polling |
| `http://localhost:8010/cookies/import/{bookmaker}` | POST | Import cookies |
| `http://localhost:8010/cookies/{bookmaker}` | DELETE | Clear cookies |

---

## Troubleshooting

### Cookies Expired
If scraping fails with 401/403 errors, your session expired. Re-export and re-import cookies.

### Check Logs
```powershell
docker logs arb-desk-market_feed-1 --tail 100
docker logs arb-desk-slack_notifier-1 --tail 50
```

### Restart Services
```powershell
docker compose restart market_feed
docker compose restart slack_notifier
```

### Full Reset
```powershell
docker compose down
docker compose up -d --force-recreate
```

---

## Architecture

```
┌─────────────────┐     ┌──────────────┐     ┌───────────┐
│  market_feed    │────▶│  odds_ingest │────▶│  arb_math │
│  (scrapes odds) │     │  (stores)    │     │  (calcs)  │
└─────────────────┘     └──────────────┘     └───────────┘
                                                   │
                                                   ▼
┌─────────────────┐     ┌──────────────────────────────────┐
│ slack_notifier  │◀────│       decision_gateway           │
│ (alerts you)    │     │  (filters profitable arbs)       │
└─────────────────┘     └──────────────────────────────────┘
```

### Service Ports
| Service | Internal Port | External Port |
|---------|---------------|---------------|
| market_feed | 8000 | 8010 |
| odds_ingest | 8000 | 8011 |
| arb_math | 8000 | 8012 |
| decision_gateway | 8000 | 8013 |
| slack_notifier | 8000 | 8014 |
| postgres | 5432 | 5433 |

---

## Auto-Polling Behavior

- **Interval**: Random 45-90 seconds between scrapes
- **Stagger**: Each bookmaker starts with a random 5-30 second offset
- **Purpose**: Avoids detection by sportsbook anti-bot systems

The system is **fully automated** once cookies are imported. You don't need to run any commands — just wait for Slack alerts when arbs are found.

