"""
Live/In-Play Odds Poller - Fast polling for live events.

Implements rapid 3-10 second polling with jitter for live/in-play markets.
Detects steam moves and rapid odds changes.
"""
from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .adapters.playwright_adapter import PlaywrightFeedAdapter

from shared.schemas import FeedConfig, MarketOdds, ScrapeResult

logger = logging.getLogger(__name__)


@dataclass
class OddsSnapshot:
    """A point-in-time snapshot of odds for tracking changes."""
    timestamp: datetime
    odds: List[MarketOdds]
    
    def get_odds_map(self) -> Dict[str, float]:
        """Get a map of selection key -> odds for comparison."""
        return {
            f"{o.event_id}:{o.selection}:{o.bookmaker}": o.odds_decimal
            for o in self.odds
        }


@dataclass
class SteamMove:
    """Detected rapid odds movement (steam move)."""
    event_id: str
    selection: str
    bookmaker: str
    old_odds: float
    new_odds: float
    change_percent: float
    detected_at: datetime = field(default_factory=datetime.utcnow)
    market_type: str = "moneyline"

    @property
    def direction(self) -> str:
        return "shortening" if self.new_odds < self.old_odds else "drifting"

    @property
    def expires_at(self) -> datetime:
        """Steam moves expire quickly - 30 seconds."""
        return self.detected_at + timedelta(seconds=30)

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def urgency_score(self) -> int:
        """Higher score = more urgent (0-100)."""
        score = 50  # Base score
        # Bigger move = more urgent
        score += min(self.change_percent * 5, 30)
        # Shortening odds = sharp money = more urgent
        if self.direction == "shortening":
            score += 10
        # Time decay - less urgent as it ages
        age_seconds = (datetime.utcnow() - self.detected_at).total_seconds()
        score -= min(age_seconds * 2, 20)
        return int(max(0, min(100, score)))


class LiveOddsPoller:
    """
    Fast polling loop for live/in-play events.
    
    Features:
    - Configurable poll interval (3-15 seconds) with jitter
    - Snapshot history for trend detection (last 5 minutes)
    - Steam move detection (>5% odds change within snapshots)
    - Automatic callback on new odds/steam moves
    """
    
    def __init__(
        self,
        adapter: "PlaywrightFeedAdapter",
        config: FeedConfig,
        on_odds_callback: Optional[callable] = None,
        on_steam_move_callback: Optional[callable] = None,
        snapshot_window_seconds: int = 300,  # 5 minutes
        steam_threshold_percent: float = 5.0,  # 5% change = steam move
    ):
        self.adapter = adapter
        self.config = config
        self.on_odds_callback = on_odds_callback
        self.on_steam_move_callback = on_steam_move_callback
        self.snapshot_window_seconds = snapshot_window_seconds
        self.steam_threshold_percent = steam_threshold_percent
        
        # Snapshot history (deque for efficient rotating buffer)
        self._snapshots: Deque[OddsSnapshot] = deque(maxlen=100)
        
        # Polling state
        self._running = False
        self._poll_count = 0
        self._error_count = 0
        self._last_poll_at: Optional[datetime] = None
        self._steam_moves_detected: List[SteamMove] = []
    
    @property
    def is_running(self) -> bool:
        return self._running
    
    @property
    def bookmaker(self) -> str:
        return self.config.bookmaker
    
    async def start(self) -> None:
        """Start the live polling loop."""
        if self._running:
            logger.warning(f"[{self.bookmaker}] Live poller already running")
            return
        
        self._running = True
        logger.info(f"[{self.bookmaker}] Starting live odds poller "
                    f"(interval: {self.config.live_poll_interval_seconds}s)")
        
        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                self._error_count += 1
                logger.error(f"[{self.bookmaker}] Live poll error: {e}")
                # Wait longer on error
                await asyncio.sleep(10)
                continue
            
            # Jittered delay for stealth
            base_interval = self.config.live_poll_interval_seconds
            jitter = random.uniform(-1.0, 2.0)  # +/- variance
            delay = max(3.0, base_interval + jitter)  # Never less than 3s
            
            await asyncio.sleep(delay)
    
    async def stop(self) -> None:
        """Stop the live polling loop."""
        self._running = False
        logger.info(f"[{self.bookmaker}] Live poller stopped")
    
    async def _poll_once(self) -> None:
        """Execute a single poll iteration."""
        start_time = datetime.utcnow()
        
        # Use adapter's scrape method (handles ban detection, CAPTCHA, etc.)
        result: ScrapeResult = await self.adapter.scrape()
        
        self._poll_count += 1
        self._last_poll_at = datetime.utcnow()
        
        if not result.success:
            self._error_count += 1
            logger.warning(f"[{self.bookmaker}] Live scrape failed: {result.error}")
            return
        
        # Mark odds as live
        live_odds = []
        for odds in result.odds:
            odds_dict = odds.model_dump()
            # We can't modify the frozen model, so we track separately
            live_odds.append(odds)
        
        # Create snapshot
        snapshot = OddsSnapshot(timestamp=start_time, odds=live_odds)
        self._snapshots.append(snapshot)
        
        # Detect steam moves
        steam_moves = self._detect_steam_moves(snapshot)
        if steam_moves:
            self._steam_moves_detected.extend(steam_moves)
            if self.on_steam_move_callback:
                for move in steam_moves:
                    await self.on_steam_move_callback(move)
        
        # Callback with new odds
        if self.on_odds_callback and live_odds:
            await self.on_odds_callback(live_odds, is_live=True)
        
        logger.debug(f"[{self.bookmaker}] Live poll #{self._poll_count}: "
                     f"{len(live_odds)} odds, {len(steam_moves)} steam moves")

    def _detect_steam_moves(self, current: OddsSnapshot) -> List[SteamMove]:
        """
        Detect steam moves by comparing current snapshot to recent history.

        A steam move is when odds change significantly (>threshold%) in a short time,
        often indicating sharp money or syndicate action.
        """
        if len(self._snapshots) < 2:
            return []

        steam_moves = []
        current_odds = current.get_odds_map()

        # Compare to snapshots within the window
        cutoff = datetime.utcnow() - timedelta(seconds=self.snapshot_window_seconds)

        for snapshot in self._snapshots:
            if snapshot.timestamp < cutoff:
                continue
            if snapshot.timestamp == current.timestamp:
                continue

            old_odds = snapshot.get_odds_map()

            for key, new_value in current_odds.items():
                old_value = old_odds.get(key)
                if old_value is None:
                    continue

                # Calculate percentage change
                change_pct = abs((new_value - old_value) / old_value) * 100

                if change_pct >= self.steam_threshold_percent:
                    parts = key.split(":")
                    if len(parts) >= 3:
                        steam_moves.append(SteamMove(
                            event_id=parts[0],
                            selection=parts[1],
                            bookmaker=parts[2],
                            old_odds=old_value,
                            new_odds=new_value,
                            change_percent=change_pct,
                        ))

        # Deduplicate (same event/selection/bookmaker)
        seen = set()
        unique_moves = []
        for move in steam_moves:
            key = (move.event_id, move.selection, move.bookmaker)
            if key not in seen:
                seen.add(key)
                unique_moves.append(move)
                logger.info(f"[{self.bookmaker}] 🔥 Steam move detected: "
                           f"{move.selection} {move.direction} "
                           f"{move.old_odds:.2f} → {move.new_odds:.2f} "
                           f"({move.change_percent:.1f}%)")

        return unique_moves

    def get_recent_snapshots(
        self,
        seconds: int = 60
    ) -> List[OddsSnapshot]:
        """Get snapshots from the last N seconds."""
        cutoff = datetime.utcnow() - timedelta(seconds=seconds)
        return [s for s in self._snapshots if s.timestamp >= cutoff]

    def get_stats(self) -> Dict[str, Any]:
        """Get live poller statistics."""
        return {
            "bookmaker": self.bookmaker,
            "is_running": self._running,
            "poll_count": self._poll_count,
            "error_count": self._error_count,
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "snapshot_count": len(self._snapshots),
            "steam_moves_detected": len(self._steam_moves_detected),
            "recent_steam_moves": [
                {
                    "event_id": m.event_id,
                    "selection": m.selection,
                    "direction": m.direction,
                    "change_percent": m.change_percent,
                    "detected_at": m.detected_at.isoformat(),
                    "urgency_score": m.urgency_score,
                    "is_expired": m.is_expired,
                }
                for m in self._steam_moves_detected[-10:]  # Last 10
            ],
        }


@dataclass
class LiveArb:
    """A live arbitrage opportunity with priority scoring."""
    event_id: str
    event_name: str
    leg1_bookmaker: str
    leg1_selection: str
    leg1_odds: float
    leg2_bookmaker: str
    leg2_selection: str
    leg2_odds: float
    profit_percentage: float
    detected_at: datetime = field(default_factory=datetime.utcnow)
    has_steam_move: bool = False
    market_type: str = "moneyline"

    @property
    def expires_at(self) -> datetime:
        """Live arbs expire quickly - 30 seconds default, 15 if steam."""
        ttl = 15 if self.has_steam_move else 30
        return self.detected_at + timedelta(seconds=ttl)

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() > self.expires_at

    @property
    def seconds_remaining(self) -> int:
        remaining = (self.expires_at - datetime.utcnow()).total_seconds()
        return max(0, int(remaining))

    @property
    def priority_score(self) -> int:
        """
        Calculate priority score (0-100) for alert ordering.

        Higher score = more urgent/profitable:
        - Profit percentage: base score
        - Steam move: +15 (sharp action)
        - Market type bonuses: boosts +10, props +5
        - Time decay: -2 per second
        """
        score = 0

        # Profit contribution (up to 40 points)
        score += min(self.profit_percentage * 10, 40)

        # Steam move bonus (sharp action indicator)
        if self.has_steam_move:
            score += 15

        # Market type bonuses
        if self.market_type == "boost":
            score += 10  # Boosts are free money
        elif self.market_type == "prop":
            score += 5  # Props less efficient
        elif self.market_type == "live":
            score += 5  # Live has speed premium

        # Time decay (fresher = better)
        age_seconds = (datetime.utcnow() - self.detected_at).total_seconds()
        score -= min(age_seconds * 2, 20)

        return int(max(0, min(100, score)))


class LiveArbPrioritizer:
    """
    Prioritizes and manages live arbitrage opportunities.

    Features:
    - Priority scoring based on profit, market type, steam moves
    - Auto-expiration of stale opportunities
    - Alert tier boosting for live arbs
    """

    def __init__(self, base_alert_ttl_seconds: int = 30):
        self.base_alert_ttl = base_alert_ttl_seconds
        self._active_arbs: List[LiveArb] = []
        self._recent_steam_moves: Dict[str, SteamMove] = {}

    def add_steam_move(self, move: SteamMove) -> None:
        """Record a steam move for priority boosting."""
        key = f"{move.event_id}:{move.selection}"
        self._recent_steam_moves[key] = move

    def create_live_arb(
        self,
        event_id: str,
        event_name: str,
        leg1_bookmaker: str,
        leg1_selection: str,
        leg1_odds: float,
        leg2_bookmaker: str,
        leg2_selection: str,
        leg2_odds: float,
        profit_percentage: float,
        market_type: str = "moneyline",
    ) -> LiveArb:
        """Create a live arb and check for steam move association."""
        # Check if any leg has a recent steam move
        key1 = f"{event_id}:{leg1_selection}"
        key2 = f"{event_id}:{leg2_selection}"

        has_steam = (
            key1 in self._recent_steam_moves and
            not self._recent_steam_moves[key1].is_expired
        ) or (
            key2 in self._recent_steam_moves and
            not self._recent_steam_moves[key2].is_expired
        )

        arb = LiveArb(
            event_id=event_id,
            event_name=event_name,
            leg1_bookmaker=leg1_bookmaker,
            leg1_selection=leg1_selection,
            leg1_odds=leg1_odds,
            leg2_bookmaker=leg2_bookmaker,
            leg2_selection=leg2_selection,
            leg2_odds=leg2_odds,
            profit_percentage=profit_percentage,
            has_steam_move=has_steam,
            market_type=market_type,
        )

        self._active_arbs.append(arb)
        return arb

    def get_prioritized_arbs(self) -> List[LiveArb]:
        """Get active arbs sorted by priority score (highest first)."""
        # Remove expired arbs
        self._active_arbs = [a for a in self._active_arbs if not a.is_expired]

        # Sort by priority
        return sorted(self._active_arbs, key=lambda a: a.priority_score, reverse=True)

    def get_alert_tier(self, arb: LiveArb) -> str:
        """
        Get alert tier with live arb boosting.

        Live arbs get boosted by one tier:
        - <1.5% profit would normally be ℹ️, becomes ⚡
        - 1.5-3% would normally be ⚡, becomes 🔥
        - >3% stays 🔥
        """
        pct = arb.profit_percentage

        # Base tier
        if pct >= 3.0:
            tier = "fire"
        elif pct >= 1.5:
            tier = "lightning"
        else:
            tier = "info"

        # Boost for live (one tier up)
        if tier == "info":
            tier = "lightning"
        elif tier == "lightning":
            tier = "fire"

        # Extra indicator for steam moves
        emoji = {
            "fire": "🔥",
            "lightning": "⚡",
            "info": "ℹ️",
        }[tier]

        if arb.has_steam_move:
            emoji = "🚨" + emoji  # Steam move = extra urgency

        return emoji

    def cleanup_expired(self) -> int:
        """Remove expired arbs and steam moves. Returns count removed."""
        before = len(self._active_arbs)
        self._active_arbs = [a for a in self._active_arbs if not a.is_expired]

        # Clean up old steam moves
        self._recent_steam_moves = {
            k: v for k, v in self._recent_steam_moves.items()
            if not v.is_expired
        }

        return before - len(self._active_arbs)

    def get_stats(self) -> Dict[str, Any]:
        """Get prioritizer statistics."""
        active = [a for a in self._active_arbs if not a.is_expired]
        return {
            "active_arbs": len(active),
            "recent_steam_moves": len(self._recent_steam_moves),
            "top_priority": active[0].priority_score if active else 0,
            "avg_priority": sum(a.priority_score for a in active) / len(active) if active else 0,
            "steam_associated": sum(1 for a in active if a.has_steam_move),
        }
