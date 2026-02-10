# DigitalOcean Deployment Guide for ArbDesk

This guide walks you through deploying ArbDesk on a DigitalOcean Droplet.

## Prerequisites

- DigitalOcean account with billing enabled
- Domain name (optional, for SSL)
- Slack workspace with bot configured
- Sportsbook accounts (FanDuel, DraftKings, etc.)

---

## 1. Create a Droplet

### Recommended Specs

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| **CPU** | 2 vCPUs | 4 vCPUs |
| **RAM** | 4 GB | 8 GB |
| **Storage** | 50 GB SSD | 80 GB SSD |
| **Region** | NYC1/NYC3 | Closest to you |

> **Why 8GB RAM?** Browser automation (Playwright/Chrome) requires significant memory, especially with multiple concurrent sessions.

### Create via Console

1. Go to [DigitalOcean Droplets](https://cloud.digitalocean.com/droplets)
2. Click **Create Droplet**
3. Choose:
   - **Image**: Ubuntu 24.04 LTS
   - **Plan**: Basic → Regular → $48/mo (4 vCPU, 8GB RAM)
   - **Datacenter**: New York (NYC1 or NYC3)
   - **Authentication**: SSH Key (recommended) or Password
4. Click **Create Droplet**

### Create via CLI

```bash
doctl compute droplet create arb-desk \
  --image ubuntu-24-04-x64 \
  --size s-4vcpu-8gb \
  --region nyc1 \
  --ssh-keys YOUR_SSH_KEY_ID
```

---

## 2. Initial Server Setup

SSH into your droplet:

```bash
ssh root@YOUR_DROPLET_IP
```

### Update System

```bash
apt update && apt upgrade -y
```

### Create Non-Root User

```bash
adduser arbdesk
usermod -aG sudo arbdesk
su - arbdesk
```

### Configure Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

---

## 3. Install Docker

```bash
# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Add user to docker group
sudo usermod -aG docker arbdesk

# Install Docker Compose
sudo apt install docker-compose-plugin -y

# Verify installation
docker --version
docker compose version

# Log out and back in for group changes
exit
ssh arbdesk@YOUR_DROPLET_IP
```

---

## 4. Clone and Configure ArbDesk

```bash
# Clone repository
git clone https://github.com/YOUR_USERNAME/arb-desk.git
cd arb-desk

# Copy environment template
cp .env.example .env

# Edit configuration
nano .env
```

### Required Environment Variables

Edit `.env` with your actual values:

```bash
# Database (change password!)
POSTGRES_PASSWORD=your_secure_password_here

# Slack (required for alerts)
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/XXX/YYY/ZZZ
SLACK_DEFAULT_CHANNEL=#arb-alerts

# Sportsbook credentials
BOOKMAKER_CREDENTIALS={"fanduel":{"username":"you@email.com","password":"yourpass"}}

# Feed configs (see .env.example for full format)
FEED_CONFIGS=[{"bookmaker":"fanduel","enabled":true,...}]

# Logging (optional - defaults shown)
LOG_LEVEL=INFO
LOG_FORMAT=json
```

### Optional: AI Reasoning for Stealth Advisor

The Stealth Advisor works fully without an API key (rule-based logic). To enable LLM-enhanced reasoning, add:

```bash
# OpenAI or Azure OpenAI (optional)
AI_API_URL=https://api.openai.com/v1/chat/completions
AI_API_KEY=sk-your-openai-api-key
AI_MODEL=gpt-4o-mini
```

> **Cost:** gpt-4o-mini is ~$0.15/million input tokens. Even heavy use costs pennies/day. If omitted, the rule-based engine handles all decisions.

### Optional: Stealth Advisor Tuning

These control how aggressively the anti-ban system protects your accounts. Defaults work well for most users:

```bash
# Stealth Advisor thresholds (optional - defaults shown)
STEALTH_MAX_WIN_RATE=0.72        # Win rate that triggers cover bets (72%)
STEALTH_MAX_ARBS_PER_DAY=12      # Max arb bets per bookmaker per day
STEALTH_HEAT_DECAY_HOURS=18      # Hours for heat score to decay by half
STEALTH_COVER_BET_PROB=0.05      # Random cover bet probability (5%)
```

---

## 5. Build and Launch

```bash
# Build all services
docker compose build

# Start all services (detached)
docker compose up -d

# Verify all services are running
docker compose ps

# Check logs
docker compose logs -f
```

### Expected Output

```
NAME                STATUS
arb-desk-postgres   Up
arb-desk-arb_math   Up
arb-desk-odds_ingest Up
arb-desk-decision_gateway Up
arb-desk-slack_notifier Up
arb-desk-market_feed Up
arb-desk-browser_shadow Up
```

---

## 6. Verify Deployment

```bash
# Test service health
curl http://localhost:8001/health  # odds_ingest
curl http://localhost:8002/health  # arb_math
curl http://localhost:8004/health  # decision_gateway
curl http://localhost:8005/health  # slack_notifier
curl http://localhost:8006/health  # market_feed

# Check Stealth Advisor heat scores
curl http://localhost:8004/heat

# Trigger a test scrape
curl -X POST http://localhost:8006/scrape-all
```

---

## 7. SSL/HTTPS Setup (Optional but Recommended)

### Install Nginx

```bash
sudo apt install nginx -y
sudo ufw allow 'Nginx Full'
```

### Configure Reverse Proxy

```bash
sudo nano /etc/nginx/sites-available/arb-desk
```

Add:

```nginx
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://localhost:8001;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/arb-desk /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

### Install SSL with Certbot

```bash
sudo apt install certbot python3-certbot-nginx -y
sudo certbot --nginx -d your-domain.com
```

---

## 8. Monitoring & Maintenance

### View Logs

**Docker Compose Logs (stdout):**
```bash
# All services
docker compose logs -f

# Specific service
docker compose logs -f market_feed

# Last 100 lines
docker compose logs --tail 100 market_feed
```

**Structured Log Files (JSON):**
```bash
# View market_feed logs
docker compose exec market_feed cat /var/log/arb-desk/market_feed.log | tail -50

# View browser-specific logs
docker compose exec market_feed cat /var/log/arb-desk/browser.log | tail -50

# Parse JSON logs with jq
docker compose exec market_feed cat /var/log/arb-desk/market_feed.log | jq -r 'select(.level=="ERROR")'
```

**Via HTTP API (market_feed:8006):**
```bash
# Recent logs
curl http://localhost:8006/logs?lines=50

# Browser logs only
curl http://localhost:8006/logs/browser

# Errors only
curl http://localhost:8006/logs/errors

# Log summary/statistics
curl http://localhost:8006/logs/summary
```

**Via Slack:**
```
arb logs              # Recent logs
arb logs errors       # Only errors
arb logs browser      # Browser activity
arb logs summary      # Statistics
arb heat              # View all bookmaker heat scores
arb heat fanduel      # View heat for specific book
arb cool fanduel      # Force 24h cooling period for a book
```

### Restart Services

```bash
# Restart all
docker compose restart

# Restart specific service
docker compose restart market_feed
```

### Update Deployment

```bash
cd ~/arb-desk
git pull
docker compose build
docker compose up -d
```

### Auto-Start on Reboot

Docker services auto-restart by default. To ensure Docker starts on boot:

```bash
sudo systemctl enable docker
```

---

## 9. Backup Database

```bash
# Create backup
docker compose exec postgres pg_dump -U arb arb_desk > backup_$(date +%Y%m%d).sql

# Restore backup
cat backup_20260205.sql | docker compose exec -T postgres psql -U arb arb_desk
```

---

## 10. Troubleshooting

### Service Won't Start

```bash
# Check logs for errors
docker compose logs market_feed

# Rebuild from scratch
docker compose down
docker compose build --no-cache
docker compose up -d
```

### Out of Memory

```bash
# Check memory usage
docker stats

# Increase swap
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Browser Crashes

The `market_feed` service needs shared memory for Chrome:

```yaml
# Already configured in docker-compose.yml
shm_size: '2gb'
```

If still crashing, increase to `4gb`.

---

## Quick Reference

| Action | Command |
|--------|---------|
| Start all | `docker compose up -d` |
| Stop all | `docker compose down` |
| View logs | `docker compose logs -f` |
| Restart service | `docker compose restart market_feed` |
| Rebuild | `docker compose build && docker compose up -d` |
| Check status | `docker compose ps` |
| View heat scores | `curl http://localhost:8004/heat` |
| Force cooling | `curl -X POST http://localhost:8004/cool -H "Content-Type: application/json" -d '{"bookmaker":"fanduel","hours":24}'` |

### Slack Quick Reference

| Command | Description |
|---------|-------------|
| `arb status` | All service statuses |
| `arb start/stop/restart <service>` | Control a service |
| `arb scrape` | Trigger immediate scrape |
| `arb logs [errors\|browser\|summary]` | View logs |
| `arb heat` | View all heat scores |
| `arb heat <book>` | View specific book heat |
| `arb cool <book>` | Force 24h cooling period |

