"""
Multi-Account Credential Manager

Handles credential rotation for sportsbooks that only allow one login at a time
(like DraftKings). Rotates through 2-5 credential sets when logged out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from shared.schemas import BookmakerCredentials, MultiAccountCredentials

logger = logging.getLogger(__name__)


@dataclass
class CredentialHealth:
    """Health tracking for a single credential set."""
    username: str
    last_login_at: Optional[datetime] = None
    last_logout_at: Optional[datetime] = None
    login_failures: int = 0
    logout_count: int = 0
    is_banned: bool = False
    cooldown_until: Optional[datetime] = None
    
    @property
    def on_cooldown(self) -> bool:
        """Check if credential is on cooldown."""
        if self.cooldown_until is None:
            return False
        return datetime.utcnow() < self.cooldown_until
    
    @property
    def is_available(self) -> bool:
        """Check if credential is available for use."""
        return not self.is_banned and not self.on_cooldown


class MultiAccountManager:
    """
    Manages multiple credential sets for sportsbooks with single-login restrictions.
    
    Features:
    - Round-robin rotation through credential sets
    - Automatic rotation on logout detection
    - Cooldown periods after forced logout
    - Ban detection and credential quarantine
    - Health tracking per credential
    """
    
    # Cooldown period after forced logout (minutes)
    LOGOUT_COOLDOWN_MINUTES = 15
    
    # Max consecutive failures before quarantine
    MAX_FAILURES_BEFORE_QUARANTINE = 3
    
    def __init__(self):
        # Multi-account credentials per bookmaker
        self._multi_creds: Dict[str, MultiAccountCredentials] = {}
        
        # Health tracking per credential (bookmaker:username -> health)
        self._health: Dict[str, CredentialHealth] = {}
    
    def register(self, multi_creds: MultiAccountCredentials) -> None:
        """Register multiple credentials for a bookmaker."""
        bookmaker = multi_creds.bookmaker.lower()
        self._multi_creds[bookmaker] = multi_creds
        
        # Initialize health tracking
        for cred in multi_creds.credentials:
            key = f"{bookmaker}:{cred.username}"
            if key not in self._health:
                self._health[key] = CredentialHealth(username=cred.username)
        
        logger.info(f"[{bookmaker}] Registered {len(multi_creds.credentials)} credential sets")
    
    def get_active_credential(self, bookmaker: str) -> Optional[BookmakerCredentials]:
        """Get the currently active credential for a bookmaker."""
        bookmaker = bookmaker.lower()
        multi = self._multi_creds.get(bookmaker)
        
        if not multi or not multi.credentials:
            return None
        
        # Get current active credential
        cred = multi.credentials[multi.active_index]
        health = self._get_health(bookmaker, cred.username)
        
        # If current is not available, rotate to next
        if not health.is_available:
            return self.rotate_to_next(bookmaker)
        
        return cred
    
    def rotate_to_next(self, bookmaker: str) -> Optional[BookmakerCredentials]:
        """Rotate to the next available credential."""
        bookmaker = bookmaker.lower()
        multi = self._multi_creds.get(bookmaker)
        
        if not multi or not multi.credentials:
            return None
        
        num_creds = len(multi.credentials)
        start_index = multi.active_index
        
        # Try each credential in rotation
        for i in range(num_creds):
            next_index = (start_index + i + 1) % num_creds
            cred = multi.credentials[next_index]
            health = self._get_health(bookmaker, cred.username)
            
            if health.is_available:
                multi.active_index = next_index
                logger.info(f"[{bookmaker}] Rotated to credential: {cred.username}")
                return cred
        
        logger.warning(f"[{bookmaker}] No available credentials after rotation")
        return None
    
    def mark_login_success(self, bookmaker: str, username: str) -> None:
        """Mark a successful login."""
        health = self._get_health(bookmaker.lower(), username)
        health.last_login_at = datetime.utcnow()
        health.login_failures = 0  # Reset failures on success
        logger.debug(f"[{bookmaker}] Login success for {username}")
    
    def mark_login_failure(self, bookmaker: str, username: str) -> None:
        """Mark a failed login."""
        health = self._get_health(bookmaker.lower(), username)
        health.login_failures += 1
        
        # Quarantine after too many failures
        if health.login_failures >= self.MAX_FAILURES_BEFORE_QUARANTINE:
            health.is_banned = True
            logger.warning(f"[{bookmaker}] Credential {username} quarantined after "
                          f"{health.login_failures} failures")
    
    def mark_forced_logout(self, bookmaker: str, username: str) -> None:
        """Mark when a credential was forcibly logged out by the sportsbook."""
        health = self._get_health(bookmaker.lower(), username)
        health.last_logout_at = datetime.utcnow()
        health.logout_count += 1
        health.cooldown_until = datetime.utcnow() + timedelta(
            minutes=self.LOGOUT_COOLDOWN_MINUTES
        )
        logger.info(f"[{bookmaker}] Credential {username} on cooldown until "
                   f"{health.cooldown_until}")

    def mark_banned(self, bookmaker: str, username: str) -> None:
        """Mark a credential as banned/suspended."""
        health = self._get_health(bookmaker.lower(), username)
        health.is_banned = True
        logger.warning(f"[{bookmaker}] Credential {username} marked as BANNED")

    def unban(self, bookmaker: str, username: str) -> None:
        """Remove ban status from a credential."""
        health = self._get_health(bookmaker.lower(), username)
        health.is_banned = False
        health.login_failures = 0
        logger.info(f"[{bookmaker}] Credential {username} unbanned")

    def _get_health(self, bookmaker: str, username: str) -> CredentialHealth:
        """Get or create health tracking for a credential."""
        key = f"{bookmaker}:{username}"
        if key not in self._health:
            self._health[key] = CredentialHealth(username=username)
        return self._health[key]

    def get_stats(self, bookmaker: str) -> Dict:
        """Get statistics for a bookmaker's credential pool."""
        bookmaker = bookmaker.lower()
        multi = self._multi_creds.get(bookmaker)

        if not multi:
            return {"error": "Bookmaker not registered"}

        stats = {
            "bookmaker": bookmaker,
            "total_credentials": len(multi.credentials),
            "active_index": multi.active_index,
            "active_username": multi.credentials[multi.active_index].username if multi.credentials else None,
            "credentials": [],
        }

        for cred in multi.credentials:
            health = self._get_health(bookmaker, cred.username)
            stats["credentials"].append({
                "username": cred.username,
                "is_available": health.is_available,
                "is_banned": health.is_banned,
                "on_cooldown": health.on_cooldown,
                "login_failures": health.login_failures,
                "logout_count": health.logout_count,
                "last_login_at": health.last_login_at.isoformat() if health.last_login_at else None,
                "cooldown_until": health.cooldown_until.isoformat() if health.cooldown_until else None,
            })

        # Count available
        stats["available_count"] = sum(
            1 for c in stats["credentials"] if c["is_available"]
        )

        return stats


# Singleton instance
credential_manager = MultiAccountManager()

