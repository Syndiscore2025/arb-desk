# ArbDesk - Sports Betting Arbitrage System

**ArbDesk** is an automated sports betting arbitrage detection and execution system. It continuously scrapes odds from multiple sportsbooks, detects arbitrage opportunities (where you can bet on all outcomes and guarantee profit), alerts you via Slack, and can place bets automatically.

---

## Table of Contents

1. [What is Arbitrage Betting?](#what-is-arbitrage-betting)
2. [System Architecture](#system-architecture)
3. [Services Overview](#services-overview)
4. [Market Types Supported](#market-types-supported)
5. [How to Launch](#how-to-launch)
6. [How to Stop](#how-to-stop)
7. [Selective Service Control](#selective-service-control)
8. [API Reference](#api-reference)
9. [Slack Commands](#slack-commands)
10. [Configuration Guide](#configuration-guide)
11. [Troubleshooting](#troubleshooting)

---

## What is Arbitrage Betting?

Arbitrage betting exploits odds discrepancies between sportsbooks. When the sum of implied probabilities across all outcomes is less than 100%, you can bet on every outcome and guarantee profit regardless of the result.

**Example:**
- FanDuel: Lakers +120 (2.20 decimal) → 45.5% implied
- DraftKings: Celtics +110 (2.10 decimal) → 47.6% implied
- **Total: 93.1%** → **6.9% guaranteed profit!**

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              ArbDesk                                     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐               │
│  │ market_feed  │───▶│ odds_ingest  │───▶│   arb_math   │               │
│  │   (8006)     │    │   (8001)     │    │   (8002)     │               │
│  │              │    │              │    │              │               │
│  │ • Scraping   │    │ • Pipeline   │    │ • Detection  │               │
│  │ • Stealth    │    │ • Routing    │    │ • Stakes     │               │
│  │ • Login      │    │              │    │ • Tiers      │               │
│  └──────────────┘    └──────────────┘    └──────┬───────┘               │
│                                                  │                       │
│                      ┌──────────────┐    ┌──────▼───────┐               │
│                      │  decision_   │◀───│    slack_    │               │
│                      │   gateway    │    │   notifier   │               │
│                      │   (8004)     │    │   (8005)     │               │
│                      │              │    │              │               │
│                      │ • AI Filter  │    │ • Alerts     │               │
│                      │              │    │ • Commands   │               │
│                      └──────────────┘    └──────────────┘               │
│                                                                          │
│  ┌──────────────┐    ┌──────────────┐                                   │
│  │   postgres   │    │browser_shadow│                                   │
│  │   (5432)     │    │   (8003)     │                                   │
│  │              │    │              │                                   │
│  │ • Database   │    │ • Placeholder│                                   │
│  └──────────────┘    └──────────────┘                                   │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Services Overview

| Service | Port | Purpose |
|---------|------|---------|
| **market_feed** | 8006 | Stealth browser scraping with Playwright |
| **odds_ingest** | 8001 | Orchestrates the full pipeline |
| **arb_math** | 8002 | Calculates arbitrage and optimal stakes |
| **decision_gateway** | 8004 | AI-powered opportunity filtering |
| **slack_notifier** | 8005 | Sends alerts, handles bet commands |
| **browser_shadow** | 8003 | Placeholder for future features |
| **postgres** | 5432 | Database for persistence |

---

## Market Types Supported

| Market Type | Typical Edge | Description |
|-------------|--------------|-------------|
| **Moneyline** | 1-3% | Standard win/lose bets |
| **Spread** | 1-3% | Point spread bets |
| **Total** | 1-3% | Over/under bets |
| **Player Props** | 2-5% | Player performance (points, rebounds, etc.) |
| **Alt Lines** | 2-6% | Alternate spreads and totals |
| **Futures** | 3-10% | Championship, MVP, division winners |
| **Boosts/Promos** | 5-20%+ | Odds boosts hedged for guaranteed profit |
| **Live/In-Play** | 3-8% | Fast-moving live game odds |
| **Parlays/SGP** | 5-15% | Correlated parlay mispricings |
| **Prediction Markets** | 2-10% | Polymarket/Kalshi vs sportsbooks |

---

## How to Launch

### Start All Services

```bash
# Start everything in background
docker compose up -d

# Verify all services are running
docker compose ps

# Watch logs in real-time
docker compose logs -f
```

### Start with Logs Visible

```bash
# Start in foreground (Ctrl+C to stop)
docker compose up
```

### Trigger a Scrape

```bash
# Scrape all enabled feeds
curl -X POST http://localhost:8006/scrape-all

# Scrape specific bookmaker
curl -X POST http://localhost:8006/scrape/fanduel
```

---

## How to Stop

### Stop All Services

```bash
# Stop and remove containers (keeps data)
docker compose down

# Stop, remove containers AND delete database
docker compose down -v
```

### Stop Specific Service

```bash
docker compose stop market_feed
```

### Pause Without Removing

```bash
docker compose pause
docker compose unpause
```

---

## Selective Service Control

### Run Only Specific Services

```bash
# Run only arb_math and odds_ingest (no scraping)
docker compose up -d postgres arb_math odds_ingest

# Run everything except market_feed
docker compose up -d postgres arb_math odds_ingest decision_gateway slack_notifier

# Add market_feed later
docker compose up -d market_feed
```

### Enable/Disable Specific Feeds

Edit your `.env` file's `FEED_CONFIGS`:

```json
[
  {"bookmaker": "fanduel", "enabled": true, ...},
  {"bookmaker": "draftkings", "enabled": false, ...},
  {"bookmaker": "fanatics", "enabled": true, ...}
]
```

Then restart:

```bash
docker compose restart market_feed
```

### Scale Services

```bash
# Run 2 instances of arb_math for load balancing
docker compose up -d --scale arb_math=2
```

---

## API Reference

### Health Checks

```bash
# Check any service
curl http://localhost:8001/health  # odds_ingest
curl http://localhost:8002/health  # arb_math
curl http://localhost:8006/health  # market_feed
```

### Market Feed API (Port 8006)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/feeds` | GET | List all configured feeds |
| `/scrape/{bookmaker}` | POST | Trigger scrape for specific book |
| `/scrape-all` | POST | Trigger scrape for all enabled feeds |
| `/bet/place` | POST | Place a bet via browser automation |

### Arb Math API (Port 8002)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/arbitrage` | POST | Calculate arbitrage from odds array |

### Odds Ingest API (Port 8001)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/process` | POST | Process odds through full pipeline |

---

## Slack Commands

### Receiving Alerts

Alerts appear in your configured Slack channel with this format:

```
🔥🔥🔥 5.2% ARBITRAGE DETECTED

📍 Lakers vs Celtics
⏰ Live - Q3 5:42

💰 Bet Breakdown ($1000 total):
  • FanDuel: Lakers ML @ 2.20 → $476.19
  • DraftKings: Celtics ML @ 2.10 → $523.81

✅ Guaranteed Profit: $52.00

💬 Reply with stake: bet a7b2c3d4 500
```

### Placing Bets

Reply to an alert with:

```
bet <alert_id> <amount>
```

**Examples:**
```
bet a7b2c3d4 500      # Bet $500 total
bet a7b2c3d4 1000     # Bet $1000 total
```

The system will:
1. Scale stakes proportionally
2. Open browsers for each bookmaker
3. Navigate to bet slips
4. Place bets automatically
5. Confirm in Slack

### Service Control Commands

Control ArbDesk services directly from Slack:

| Command | Description |
|---------|-------------|
| `arb status` | Show status of all services |
| `arb start <service>` | Start a stopped service |
| `arb stop <service>` | Stop a running service |
| `arb restart <service>` | Restart a service |
| `arb scrape` | Trigger a market scrape |

**Available Services:**
- `market_feed` - Browser scraping
- `odds_ingest` - Pipeline orchestrator
- `arb_math` - Arbitrage calculator
- `decision_gateway` - AI filtering
- `browser_shadow` - Placeholder

**Examples:**
```
arb status              # Show all service statuses
arb stop market_feed    # Stop the scraper
arb start market_feed   # Start the scraper
arb restart arb_math    # Restart arb calculator
arb scrape              # Trigger immediate scrape
```

**Status Response:**
```
📊 ArbDesk Service Status

🟢 arb_math: running
🟢 decision_gateway: running
🟢 market_feed: running
🟢 odds_ingest: running
🟢 postgres: running
🟢 slack_notifier: running
```

---

## Configuration Guide

### Feed Configuration Structure

```json
{
  "bookmaker": "fanduel",
  "enabled": true,
  "poll_interval_seconds": 30,
  "login_url": "https://sportsbook.fanduel.com/login",
  "odds_urls": [
    "https://sportsbook.fanduel.com/navigation/nba",
    "https://sportsbook.fanduel.com/navigation/nfl"
  ],
  "sports": ["nba", "nfl", "mlb"],
  "markets": ["moneyline", "spread", "total", "prop"],
  "headless": true,
  "extra_config": {
    "username_selector": "input[type='email']",
    "password_selector": "input[type='password']",
    "submit_selector": "button[type='submit']",
    "login_success_selector": "[data-test-id='user-menu']",
    "event_container_selector": "[data-test-id='event-card']",
    "selection_selector": "[data-test-id='outcome']",
    "odds_selector": "[data-test-id='odds']"
  }
}
```

### Alert Tiers

| Tier | Emoji | Profit % | Priority |
|------|-------|----------|----------|
| Fire | 🔥🔥🔥 | ≥ 3.0% | Highest |
| Lightning | ⚡⚡ | 1.5% - 3.0% | Medium |
| Info | ℹ️ | < 1.5% | Lowest |

**Live arbs are boosted one tier** (e.g., 2% live = 🔥 instead of ⚡)

---

## Troubleshooting

### Common Issues

**Services won't start:**
```bash
docker compose logs <service_name>
docker compose down && docker compose up -d
```

**Browser crashes (market_feed):**
- Ensure `shm_size: '2gb'` in docker-compose.yml
- Check memory: `docker stats`

**No alerts appearing:**
1. Check Slack tokens in `.env`
2. Verify feeds are enabled: `curl http://localhost:8006/feeds`
3. Check logs: `docker compose logs slack_notifier`

**Login failures:**
- Verify credentials in `BOOKMAKER_CREDENTIALS`
- Check for CAPTCHA: `docker compose logs market_feed`
- Add `CAPTCHA_API_KEY` for auto-solving

**Rate limited / Banned:**
- Add residential proxies to `PROXY_LIST`
- Increase `poll_interval_seconds`
- Enable headless mode: `"headless": true`

### Useful Commands

```bash
# View all logs
docker compose logs -f

# View specific service logs
docker compose logs -f market_feed

# Check resource usage
docker stats

# Restart everything
docker compose restart

# Full rebuild
docker compose down
docker compose build --no-cache
docker compose up -d

# Enter container shell
docker compose exec market_feed bash
```

---

## Quick Reference Card

| Action | Command |
|--------|---------|
| **Start all** | `docker compose up -d` |
| **Stop all** | `docker compose down` |
| **View logs** | `docker compose logs -f` |
| **Restart service** | `docker compose restart market_feed` |
| **Check status** | `docker compose ps` |
| **Trigger scrape** | `curl -X POST http://localhost:8006/scrape-all` |
| **List feeds** | `curl http://localhost:8006/feeds` |
| **Run tests** | `python -m pytest tests/ -v` |

---

## File Structure

```
arb-desk/
├── docker-compose.yml      # Service orchestration
├── .env                    # Environment configuration
├── .env.example            # Template with all variables
├── shared/
│   └── schemas.py          # Shared Pydantic models
├── services/
│   ├── odds_ingest/        # Pipeline orchestrator
│   ├── arb_math/           # Arbitrage calculator
│   ├── market_feed/        # Browser scraping
│   │   └── app/
│   │       ├── main.py
│   │       ├── stealth_playwright.py
│   │       ├── live_poller.py
│   │       ├── bet_executor.py
│   │       ├── credential_manager.py
│   │       ├── scrapers/   # Props, alt lines, futures, boosts
│   │       └── adapters/   # CT sportsbooks, Pinnacle, prediction markets
│   ├── decision_gateway/   # AI filtering
│   ├── slack_notifier/     # Alerts & commands
│   └── browser_shadow/     # Placeholder
├── tests/                  # Test suite (90 tests)
├── ARB-DESK.md            # This documentation
└── DIGITALOCEAN-SETUP.md  # Deployment guide
```

