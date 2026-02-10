"""
Stealth Advisor - AI reasoning agent for avoiding sportsbook detection.

This agent analyzes betting patterns and strategically recommends:
- When to SKIP +EV arbs to appear recreational
- When to place "cover bets" (intentional small losses)
- How to randomize bet timing and amounts
- Per-bookmaker heat tracking and cooling strategies

The goal is to maximize long-term profit by extending account longevity,
even if it means sacrificing some short-term +EV opportunities.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import httpx

from shared.schemas import ArbOpportunity, DecisionResponse

logger = logging.getLogger(__name__)

# Azure OpenAI / OpenAI configuration
AI_API_URL = os.getenv("AI_API_URL")
AI_API_KEY = os.getenv("AI_API_KEY")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")

# Stealth thresholds
MAX_WIN_RATE_BEFORE_COOLING = 0.65  # 65% win rate triggers cooling
MAX_ARB_FREQUENCY_PER_DAY = 5  # Max arb bets per book per day
HEAT_DECAY_HOURS = 24  # Hours for heat to decay by half
COVER_BET_PROBABILITY = 0.15  # 15% chance to suggest a cover bet


@dataclass
class BookmakerProfile:
    """Tracks betting history and heat for a single bookmaker."""
    bookmaker: str
    total_bets: int = 0
    arb_bets: int = 0
    wins: int = 0
    losses: int = 0
    total_wagered: float = 0.0
    total_profit: float = 0.0
    last_bet_at: Optional[datetime] = None
    last_arb_at: Optional[datetime] = None
    arb_bets_today: int = 0
    today_date: Optional[str] = None
    heat_score: float = 0.0  # 0-100, higher = more suspicious
    cooling_until: Optional[datetime] = None
    consecutive_wins: int = 0
    bet_history: List[Dict[str, Any]] = field(default_factory=list)

    def record_bet(self, is_arb: bool, stake: float, profit: float, won: bool) -> None:
        """Record a bet and update metrics."""
        now = datetime.utcnow()
        today = now.strftime("%Y-%m-%d")

        # Reset daily counter if new day
        if self.today_date != today:
            self.today_date = today
            self.arb_bets_today = 0

        self.total_bets += 1
        self.total_wagered += stake

        if is_arb:
            self.arb_bets += 1
            self.arb_bets_today += 1
            self.last_arb_at = now

        if won:
            self.wins += 1
            self.consecutive_wins += 1
            self.total_profit += profit
        else:
            self.losses += 1
            self.consecutive_wins = 0
            self.total_profit -= stake  # Loss

        self.last_bet_at = now
        self._update_heat_score()

        # Keep last 100 bets for pattern analysis
        self.bet_history.append({
            "timestamp": now.isoformat(),
            "is_arb": is_arb,
            "stake": stake,
            "won": won,
            "profit": profit,
        })
        if len(self.bet_history) > 100:
            self.bet_history.pop(0)

    def _update_heat_score(self) -> None:
        """Calculate heat score based on suspicious patterns."""
        heat = 0.0

        # Win rate factor (0-30 points)
        if self.total_bets >= 10:
            win_rate = self.wins / self.total_bets
            if win_rate > 0.55:
                heat += (win_rate - 0.55) * 100  # Up to 30 for 85% win rate

        # Arb frequency factor (0-30 points)
        if self.total_bets > 0:
            arb_ratio = self.arb_bets / self.total_bets
            heat += arb_ratio * 30

        # Daily arb frequency (0-20 points)
        heat += min(self.arb_bets_today * 4, 20)

        # Consecutive wins (0-20 points)
        heat += min(self.consecutive_wins * 4, 20)

        self.heat_score = min(heat, 100)

    @property
    def win_rate(self) -> float:
        """Current win rate."""
        if self.total_bets == 0:
            return 0.0
        return self.wins / self.total_bets

    @property
    def is_hot(self) -> bool:
        """Check if account is running hot (suspicious)."""
        return self.heat_score >= 60

    @property
    def needs_cooling(self) -> bool:
        """Check if account needs a cooling period."""
        if self.cooling_until and datetime.utcnow() < self.cooling_until:
            return True
        return self.heat_score >= 80

    def start_cooling(self, hours: int = 24) -> None:
        """Start a cooling period."""
        self.cooling_until = datetime.utcnow() + timedelta(hours=hours)
        logger.info(f"[{self.bookmaker}] Starting {hours}h cooling period (heat={self.heat_score:.1f})")

    def decay_heat(self) -> None:
        """Apply time-based heat decay."""
        if self.last_bet_at:
            hours_since = (datetime.utcnow() - self.last_bet_at).total_seconds() / 3600
            decay_factor = 0.5 ** (hours_since / HEAT_DECAY_HOURS)
            self.heat_score *= decay_factor



class StealthAdvisor:
    """
    AI-powered reasoning agent that advises on bet placement strategy
    to avoid sportsbook detection and account limiting.

    Analyzes:
    - Per-bookmaker heat scores and betting patterns
    - Opportunity quality vs. detection risk
    - Optimal timing and stake sizing
    - When to place cover bets or skip opportunities
    """

    def __init__(self):
        self._profiles: Dict[str, BookmakerProfile] = {}

    def get_profile(self, bookmaker: str) -> BookmakerProfile:
        """Get or create a bookmaker profile."""
        if bookmaker not in self._profiles:
            self._profiles[bookmaker] = BookmakerProfile(bookmaker=bookmaker)
        profile = self._profiles[bookmaker]
        profile.decay_heat()  # Apply time decay on access
        return profile

    async def evaluate(self, opportunity: ArbOpportunity, context: Dict[str, Any]) -> DecisionResponse:
        """
        Evaluate an arb opportunity through the stealth lens.

        Returns a decision with reasoning about whether to:
        - TAKE: Place the arb bet
        - SKIP: Pass on this opportunity
        - COVER: Place a cover bet instead
        - DELAY: Wait before placing
        - COOL: Account needs cooling period
        """
        # Extract bookmakers from legs
        bookmakers = [leg.get("bookmaker", "") for leg in opportunity.legs]
        profiles = {bm: self.get_profile(bm) for bm in bookmakers if bm}

        # Build analysis context
        analysis = self._build_analysis(opportunity, profiles)

        # Check hard limits first
        hard_block = self._check_hard_limits(profiles, analysis)
        if hard_block:
            return hard_block

        # Use AI reasoning if available, otherwise use rule-based logic
        if AI_API_URL and AI_API_KEY:
            return await self._ai_reasoning(opportunity, analysis, profiles)
        else:
            return self._rule_based_reasoning(opportunity, analysis, profiles)

    def _build_analysis(
        self,
        opportunity: ArbOpportunity,
        profiles: Dict[str, BookmakerProfile],
    ) -> Dict[str, Any]:
        """Build a comprehensive analysis context for reasoning."""
        profit_pct = opportunity.profit_percentage or 0
        now = datetime.utcnow()

        # Time analysis
        hour = now.hour
        is_peak_hours = 10 <= hour <= 23  # Normal betting hours
        is_off_hours = hour < 7 or hour > 1  # Suspicious hours

        # Bookmaker heat summary
        heat_summary = {}
        max_heat = 0
        for bm, profile in profiles.items():
            heat_summary[bm] = {
                "heat_score": round(profile.heat_score, 1),
                "win_rate": round(profile.win_rate, 3),
                "total_bets": profile.total_bets,
                "arb_bets_today": profile.arb_bets_today,
                "consecutive_wins": profile.consecutive_wins,
                "is_hot": profile.is_hot,
                "needs_cooling": profile.needs_cooling,
            }
            max_heat = max(max_heat, profile.heat_score)

        # Opportunity quality
        quality = "low"
        if profit_pct >= 3.0:
            quality = "high"
        elif profit_pct >= 1.5:
            quality = "medium"

        return {
            "profit_pct": profit_pct,
            "quality": quality,
            "is_live": opportunity.is_live,
            "market": opportunity.market,
            "hour": hour,
            "is_peak_hours": is_peak_hours,
            "is_off_hours": is_off_hours,
            "heat_summary": heat_summary,
            "max_heat": max_heat,
            "bookmaker_count": len(profiles),
        }

    def _check_hard_limits(
        self,
        profiles: Dict[str, BookmakerProfile],
        analysis: Dict[str, Any],
    ) -> Optional[DecisionResponse]:
        """Check hard limits that should always block betting."""

        # Any bookmaker needs cooling
        for bm, profile in profiles.items():
            if profile.needs_cooling:
                remaining = ""
                if profile.cooling_until:
                    mins = int((profile.cooling_until - datetime.utcnow()).total_seconds() / 60)
                    remaining = f" ({mins}min remaining)"
                return DecisionResponse(
                    decision="cool",
                    rationale=(
                        f"🧊 COOLING REQUIRED for {bm}{remaining}. "
                        f"Heat score: {profile.heat_score:.0f}/100. "
                        f"Win rate: {profile.win_rate:.0%}. "
                        f"Consecutive wins: {profile.consecutive_wins}. "
                        f"Placing bets now would risk account limiting."
                    ),
                )

        # Daily arb limit exceeded
        for bm, profile in profiles.items():
            if profile.arb_bets_today >= MAX_ARB_FREQUENCY_PER_DAY:
                return DecisionResponse(
                    decision="skip",
                    rationale=(
                        f"⏸️ Daily arb limit reached for {bm} "
                        f"({profile.arb_bets_today}/{MAX_ARB_FREQUENCY_PER_DAY}). "
                        f"More arb bets today would create a detectable pattern. "
                        f"Consider placing a recreational bet instead."
                    ),
                )

        return None

    def _rule_based_reasoning(
        self,
        opportunity: ArbOpportunity,
        analysis: Dict[str, Any],
        profiles: Dict[str, BookmakerProfile],
    ) -> DecisionResponse:
        """
        Rule-based stealth reasoning when no AI API is configured.

        Strategy priorities:
        1. High-value arbs (>3%) - almost always take, but watch heat
        2. Medium arbs (1.5-3%) - take if heat is low, sometimes skip
        3. Low arbs (<1.5%) - skip more often, not worth the heat
        """
        profit_pct = analysis["profit_pct"]
        max_heat = analysis["max_heat"]
        quality = analysis["quality"]
        is_live = analysis["is_live"]
        reasons = []

        # ── Strategic skip probability based on heat + quality ──
        skip_chance = self._calculate_skip_probability(max_heat, quality, is_live)

        # Roll the dice - sometimes we intentionally pass
        if random.random() < skip_chance:
            hottest_bm = max(profiles.keys(), key=lambda b: profiles[b].heat_score)
            profile = profiles[hottest_bm]
            reasons.append(
                f"🎲 Strategic skip ({skip_chance:.0%} skip probability). "
                f"Profit: {profit_pct:.2f}% ({quality}). "
                f"Heat on {hottest_bm}: {profile.heat_score:.0f}/100. "
                f"Win rate: {profile.win_rate:.0%}. "
                f"Skipping to maintain a recreational betting pattern."
            )
            return DecisionResponse(decision="skip", rationale=" ".join(reasons))

        # ── Cover bet suggestion ──
        if self._should_suggest_cover(profiles):
            cover = self._generate_cover_bet_suggestion(profiles)
            reasons.append(
                f"🎭 COVER BET RECOMMENDED before taking this arb. "
                f"{cover['suggestion']}. "
                f"This breaks the pattern of only betting +EV lines."
            )
            return DecisionResponse(decision="cover_then_take", rationale=" ".join(reasons))

        # ── Delay suggestion ──
        delay_seconds = self._calculate_delay(analysis, profiles)
        if delay_seconds > 0:
            reasons.append(f"⏱️ Delay {delay_seconds}s before placing.")

        # ── Stake adjustment ──
        stake_modifier = self._calculate_stake_modifier(max_heat, quality)

        # ── Take the bet ──
        heat_status = ", ".join(f"{bm}={p.heat_score:.0f}" for bm, p in profiles.items())
        reasons.append(
            f"✅ TAKE this {quality} arb ({profit_pct:.2f}%). "
            f"Heat: [{heat_status}]. Stake modifier: {stake_modifier:.0%}."
        )
        if delay_seconds > 0:
            reasons.append(f"Wait {delay_seconds}s before placing.")
        if stake_modifier < 1.0:
            reasons.append(f"Reduce stake to {stake_modifier:.0%} to lower profile.")

        return DecisionResponse(decision="take", rationale=" ".join(reasons))

    def _calculate_skip_probability(
        self, max_heat: float, quality: str, is_live: bool
    ) -> float:
        """
        Calculate the probability of strategically skipping an opportunity.
        Higher heat = skip more. Lower quality = skip more.
        Live bets are riskier so skip more.
        """
        base_skip = 0.0

        # Quality-based skip rate
        if quality == "high":
            base_skip = 0.05  # Only skip 5% of >3% arbs
        elif quality == "medium":
            base_skip = 0.15  # Skip 15% of 1.5-3% arbs
        else:
            base_skip = 0.35  # Skip 35% of <1.5% arbs (not worth heat)

        # Heat modifier - skip more when running hot
        if max_heat > 60:
            base_skip += 0.20
        elif max_heat > 40:
            base_skip += 0.10
        elif max_heat > 20:
            base_skip += 0.05

        # Live betting is riskier for detection
        if is_live:
            base_skip += 0.10

        return min(base_skip, 0.80)  # Never skip more than 80%

    def _should_suggest_cover(self, profiles: Dict[str, BookmakerProfile]) -> bool:
        """Determine if a cover bet should be placed before the arb."""
        for profile in profiles.values():
            # Suggest cover if win rate is too high
            if profile.total_bets >= 10 and profile.win_rate > MAX_WIN_RATE_BEFORE_COOLING:
                return True
            # Suggest cover after consecutive wins
            if profile.consecutive_wins >= 4:
                return True
            # Random cover bet to break patterns
            if random.random() < COVER_BET_PROBABILITY:
                return True
        return False

    def _generate_cover_bet_suggestion(
        self, profiles: Dict[str, BookmakerProfile]
    ) -> Dict[str, Any]:
        """Generate a cover bet suggestion to look recreational."""
        hottest_bm = max(profiles.keys(), key=lambda b: profiles[b].heat_score)

        cover_types = [
            {
                "suggestion": f"Place a small ($5-15) parlay on {hottest_bm} "
                              f"on a popular game (heavy favorite + over/under)",
                "type": "parlay",
                "amount_range": (5, 15),
            },
            {
                "suggestion": f"Place a $10-25 moneyline bet on {hottest_bm} "
                              f"on a heavy favorite (-300 or more)",
                "type": "favorite_ml",
                "amount_range": (10, 25),
            },
            {
                "suggestion": f"Place a $5-10 player prop bet on {hottest_bm} "
                              f"on a star player's points/yards",
                "type": "player_prop",
                "amount_range": (5, 10),
            },
            {
                "suggestion": f"Place a $10-20 same-game parlay on {hottest_bm} "
                              f"to look like a casual bettor",
                "type": "sgp",
                "amount_range": (10, 20),
            },
        ]

        return random.choice(cover_types)

    def _calculate_delay(
        self, analysis: Dict[str, Any], profiles: Dict[str, BookmakerProfile]
    ) -> int:
        """Calculate delay in seconds before placing bet."""
        delay = 0

        # Off-hours betting is suspicious
        if analysis.get("is_off_hours"):
            delay += random.randint(30, 120)

        # High heat = more delay
        max_heat = analysis.get("max_heat", 0)
        if max_heat > 50:
            delay += random.randint(15, 60)

        # Check if betting too fast after last bet
        for profile in profiles.values():
            if profile.last_bet_at:
                seconds_since = (datetime.utcnow() - profile.last_bet_at).total_seconds()
                if seconds_since < 60:
                    delay += random.randint(30, 90)

        return delay

    def _calculate_stake_modifier(self, max_heat: float, quality: str) -> float:
        """Calculate stake reduction factor based on heat and quality."""
        if max_heat > 70:
            return 0.5  # Reduce to 50%
        if max_heat > 50:
            return 0.75  # Reduce to 75%
        if quality == "low":
            return 0.8  # Small bets on low-quality arbs
        return 1.0

    async def _ai_reasoning(
        self,
        opportunity: ArbOpportunity,
        analysis: Dict[str, Any],
        profiles: Dict[str, BookmakerProfile],
    ) -> DecisionResponse:
        """
        Use LLM reasoning for stealth advice.
        Falls back to rule-based if API call fails.
        """
        prompt = f"""You are a stealth betting advisor. Your goal is to maximize long-term profit by extending account longevity, even if it means sacrificing some short-term +EV opportunities.

OPPORTUNITY:
- Profit: {analysis['profit_pct']:.2f}% ({analysis['quality']} quality)
- Market: {opportunity.market}
- Is Live: {analysis['is_live']}
- Time: {analysis['hour']}:00 ({'peak hours' if analysis['is_peak_hours'] else 'off hours'})

BOOKMAKER HEAT SCORES:
{json.dumps(analysis['heat_summary'], indent=2)}

STRATEGY GUIDELINES:
- High heat (>60): Skip more opportunities, suggest cover bets
- High win rate (>65%): Account needs cover bets to look recreational
- Daily arb limit: {MAX_ARB_FREQUENCY_PER_DAY} per bookmaker
- Low-quality arbs (<1.5%): Not worth the heat, skip more often
- Live bets: More suspicious, higher skip probability

DECISION OPTIONS:
1. "take" - Place the arb bet (with optional delay/stake modifier)
2. "skip" - Pass on this opportunity to maintain recreational pattern
3. "cover_then_take" - Place a cover bet first, then take the arb
4. "delay" - Wait before placing (specify seconds)

Respond with JSON:
{{"decision": "take|skip|cover_then_take|delay", "rationale": "detailed explanation", "delay_seconds": 0, "stake_modifier": 1.0, "cover_bet": null}}
"""

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    AI_API_URL,
                    headers={"Authorization": f"Bearer {AI_API_KEY}"},
                    json={
                        "model": AI_MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                response.raise_for_status()
                data = response.json()

                # Parse response
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "{}")
                result = json.loads(content)

                decision = result.get("decision", "take")
                rationale = result.get("rationale", "AI reasoning completed.")

                # Add heat context to rationale
                heat_status = ", ".join(
                    f"{bm}={p.heat_score:.0f}" for bm, p in profiles.items()
                )
                full_rationale = f"🤖 AI STEALTH ADVICE: {rationale} [Heat: {heat_status}]"

                return DecisionResponse(decision=decision, rationale=full_rationale)

        except Exception as e:
            logger.warning(f"AI reasoning failed, falling back to rule-based: {e}")
            return self._rule_based_reasoning(opportunity, analysis, profiles)

    def get_all_heat_scores(self) -> Dict[str, Dict[str, Any]]:
        """Get heat scores for all tracked bookmakers."""
        result = {}
        for bm, profile in self._profiles.items():
            profile.decay_heat()  # Apply decay
            result[bm] = {
                "heat_score": round(profile.heat_score, 1),
                "win_rate": round(profile.win_rate, 3),
                "total_bets": profile.total_bets,
                "arb_bets": profile.arb_bets,
                "arb_bets_today": profile.arb_bets_today,
                "consecutive_wins": profile.consecutive_wins,
                "is_hot": profile.is_hot,
                "needs_cooling": profile.needs_cooling,
                "cooling_until": profile.cooling_until.isoformat() if profile.cooling_until else None,
                "last_bet_at": profile.last_bet_at.isoformat() if profile.last_bet_at else None,
            }
        return result

    def record_bet_result(
        self, bookmaker: str, is_arb: bool, stake: float, profit: float, won: bool
    ) -> None:
        """Record a bet result for a bookmaker."""
        profile = self.get_profile(bookmaker)
        profile.record_bet(is_arb=is_arb, stake=stake, profit=profit, won=won)
        logger.info(
            f"[{bookmaker}] Recorded bet: arb={is_arb}, stake=${stake:.2f}, "
            f"won={won}, heat={profile.heat_score:.1f}"
        )

    def force_cooling(self, bookmaker: str, hours: int = 24) -> None:
        """Force a bookmaker into cooling period."""
        profile = self.get_profile(bookmaker)
        profile.start_cooling(hours=hours)
