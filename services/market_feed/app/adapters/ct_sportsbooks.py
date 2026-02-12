"""
Connecticut Legal Sportsbooks Configuration

As of 2026, Connecticut has 3 legal online sportsbooks:
1. FanDuel - Partnered with Mohegan Digital
2. DraftKings - Partnered with MPI Master Wagering License
3. Fanatics - Partnered with CT Lottery

These configurations provide the CSS selectors and URLs needed
for the PlaywrightGenericAdapter to scrape each book.
"""
from __future__ import annotations

from typing import Any, Dict

# ─────────────────────────────────────────────────────────────────────────────
# FanDuel Connecticut
# ─────────────────────────────────────────────────────────────────────────────

FANDUEL_CT_CONFIG: Dict[str, Any] = {
    "bookmaker": "fanduel",
    "enabled": True,
    "login_url": "https://sportsbook.fanduel.com/login",
    "odds_urls": [
        # FanDuel API endpoints - these return JSON with odds data
        # The tab IDs correspond to: all games, spreads, totals, moneyline
        # NBA
        "https://sbapi.fanduel.com/sportsbook/v1/navigation/sports/nba/events?marketType=moneyline",
        # NFL
        "https://sbapi.fanduel.com/sportsbook/v1/navigation/sports/nfl/events?marketType=moneyline",
        # MLB
        "https://sbapi.fanduel.com/sportsbook/v1/navigation/sports/mlb/events?marketType=moneyline",
        # NHL
        "https://sbapi.fanduel.com/sportsbook/v1/navigation/sports/nhl/events?marketType=moneyline",
    ],
    "live_odds_urls": [
        "https://sportsbook.fanduel.com/live",
    ],
    "live_polling_enabled": True,
    "live_poll_interval_seconds": 5,
    "poll_interval_seconds": 30,
    "credential_rotation_enabled": False,
    "headless": True,
    "extra_config": {
        # Login selectors
        "username_selector": "input[type='email'], input[name='email']",
        "password_selector": "input[type='password']",
        "submit_selector": "button[type='submit'], button.login-button",
        "login_success_selector": "[data-testid='user-menu'], .user-menu, .logged-in",
        
        # 2FA selectors (if needed)
        "totp_selector": "input[name='otp'], input[placeholder*='code']",
        
        # Odds scraping selectors
        "event_container_selector": "[data-testid='event-card'], .event-card, .coupon-content",
        "event_name_selector": ".event-title, .team-name, [data-testid='event-name']",
        "selection_selector": ".outcome, .market-btn, [data-testid='outcome']",
        "selection_name_selector": ".outcome-name, .selection-name",
        "odds_selector": ".odds-value, .price, [data-testid='price']",
        "sport_selector": ".sport-name, [data-sport]",
        "market_selector": ".market-name, .market-header",
        
        # Bet placement selectors
        "stake_input_selector": "input.stake, input[data-testid='stake-input']",
        "place_bet_button_selector": "button.place-bet, [data-testid='place-bet']",
        "confirmation_selector": ".bet-confirmed, [data-testid='bet-confirmation']",
        "betslip_odds_selector": ".betslip .odds, [data-testid='betslip-odds']",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# DraftKings Connecticut
# ─────────────────────────────────────────────────────────────────────────────

DRAFTKINGS_CT_CONFIG: Dict[str, Any] = {
    "bookmaker": "draftkings",
    "enabled": True,
    "login_url": "https://sportsbook.draftkings.com/login",
    "odds_urls": [
        # NFL
        "https://sportsbook.draftkings.com/leagues/football/nfl",
        # NBA
        "https://sportsbook.draftkings.com/leagues/basketball/nba",
        # MLB
        "https://sportsbook.draftkings.com/leagues/baseball/mlb",
        # NHL
        "https://sportsbook.draftkings.com/leagues/hockey/nhl",
        # NCAA Football
        "https://sportsbook.draftkings.com/leagues/football/ncaaf",
        # NCAA Basketball
        "https://sportsbook.draftkings.com/leagues/basketball/ncaab",
    ],
    "live_odds_urls": [
        "https://sportsbook.draftkings.com/live",
    ],
    "live_polling_enabled": True,
    "live_poll_interval_seconds": 5,
    "poll_interval_seconds": 30,
    # DraftKings only allows one login at a time - need credential rotation
    "credential_rotation_enabled": True,
    "headless": True,
    "extra_config": {
        # Login selectors
        "username_selector": "input[type='email'], input#email",
        "password_selector": "input[type='password'], input#password",
        "submit_selector": "button[type='submit'], .login-button",
        "login_success_selector": ".account-menu, .user-avatar, [data-testid='user-menu']",
        
        # 2FA selectors
        "totp_selector": "input[name='verificationCode'], input.otp-input",
        
        # Odds scraping selectors
        "event_container_selector": ".sportsbook-event-accordion__wrapper, .event-card",
        "event_name_selector": ".event-cell__name, .team-name",
        "selection_selector": ".sportsbook-outcome-cell, .outcome-cell",
        "selection_name_selector": ".sportsbook-outcome-cell__label, .outcome-name",
        "odds_selector": ".sportsbook-odds, .odds-american, .odds-value",
        "sport_selector": ".sport-title, [data-sport-name]",
        "market_selector": ".sportsbook-table-header, .market-name",
        
        # Bet placement selectors
        "stake_input_selector": "input.stake-input, [data-testid='stake']",
        "place_bet_button_selector": "button.place-bet, [data-testid='submit-bet']",
        "confirmation_selector": ".bet-receipt, [data-testid='bet-confirmation']",
        "betslip_odds_selector": ".betslip-odds, [data-testid='selection-odds']",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Fanatics Connecticut
# ─────────────────────────────────────────────────────────────────────────────

FANATICS_CT_CONFIG: Dict[str, Any] = {
    "bookmaker": "fanatics",
    "enabled": True,
    "login_url": "https://sportsbook.fanatics.com/login",
    "odds_urls": [
        # NFL
        "https://sportsbook.fanatics.com/sports/football/nfl",
        # NBA
        "https://sportsbook.fanatics.com/sports/basketball/nba",
        # MLB
        "https://sportsbook.fanatics.com/sports/baseball/mlb",
        # NHL
        "https://sportsbook.fanatics.com/sports/hockey/nhl",
        # NCAA Football
        "https://sportsbook.fanatics.com/sports/football/ncaaf",
        # NCAA Basketball
        "https://sportsbook.fanatics.com/sports/basketball/ncaab",
    ],
    "live_odds_urls": [
        "https://sportsbook.fanatics.com/live",
    ],
    "live_polling_enabled": True,
    "live_poll_interval_seconds": 5,
    "poll_interval_seconds": 30,
    "credential_rotation_enabled": False,
    "headless": True,
    "extra_config": {
        # Login selectors
        "username_selector": "input[type='email'], input#email",
        "password_selector": "input[type='password'], input#password",
        "submit_selector": "button[type='submit'], button.login-btn",
        "login_success_selector": ".user-profile, .account-icon, [data-testid='account']",

        # 2FA selectors
        "totp_selector": "input[name='code'], input.verification-code",

        # Odds scraping selectors
        "event_container_selector": ".event-container, .match-card, [data-testid='event']",
        "event_name_selector": ".event-name, .match-title, .team-names",
        "selection_selector": ".outcome, .bet-option, [data-testid='selection']",
        "selection_name_selector": ".outcome-label, .selection-name",
        "odds_selector": ".odds, .price-value, [data-testid='odds']",
        "sport_selector": ".sport-label, [data-sport]",
        "market_selector": ".market-title, .bet-type",

        # Bet placement selectors
        "stake_input_selector": "input.stake, input[name='stake']",
        "place_bet_button_selector": "button.place-bet, .submit-bet-btn",
        "confirmation_selector": ".bet-success, .confirmation-message",
        "betslip_odds_selector": ".slip-odds, .selected-odds",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# All CT Sportsbook Configs
# ─────────────────────────────────────────────────────────────────────────────

CT_SPORTSBOOK_CONFIGS = {
    "fanduel": FANDUEL_CT_CONFIG,
    "draftkings": DRAFTKINGS_CT_CONFIG,
    "fanatics": FANATICS_CT_CONFIG,
}


def get_ct_config(bookmaker: str) -> Dict[str, Any]:
    """Get configuration for a CT sportsbook by name."""
    return CT_SPORTSBOOK_CONFIGS.get(bookmaker.lower(), {})


def get_all_ct_configs() -> Dict[str, Dict[str, Any]]:
    """Get all CT sportsbook configurations."""
    return CT_SPORTSBOOK_CONFIGS.copy()

