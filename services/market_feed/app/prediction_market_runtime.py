from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from datetime import datetime
from typing import Any, Dict, List, Optional


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp_path, path)


class PredictionMarketRuntimeStore:
    def __init__(
        self,
        state_path: Optional[str] = None,
        history_limit: int = 25,
        payload_sample_size: int = 25,
    ):
        self.state_path = state_path or os.getenv(
            "PREDICTION_MARKET_RUNTIME_STATE_PATH",
            os.path.join("data", "prediction_markets", "runtime_state.json"),
        )
        self.history_limit = max(1, int(history_limit))
        self.payload_sample_size = max(0, int(payload_sample_size))
        self._history: deque[Dict[str, Any]] = deque(maxlen=self.history_limit)
        self._summary: Dict[str, Any] = {
            "last_cycle_at": None,
            "last_push_at": None,
            "last_status": None,
            "last_error": None,
        }
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            summary = payload.get("summary") or {}
            history = payload.get("history") or []
            if isinstance(summary, dict):
                self._summary.update(summary)
            if isinstance(history, list):
                self._history.clear()
                for entry in history[-self.history_limit :]:
                    if isinstance(entry, dict):
                        self._history.append(entry)
        except Exception:
            return

    def _save(self) -> None:
        try:
            _atomic_write_json(
                self.state_path,
                {
                    "summary": dict(self._summary),
                    "history": list(self._history),
                    "saved_at": datetime.utcnow().isoformat(),
                },
            )
        except Exception:
            return

    def record_cycle(
        self,
        *,
        market_name: str,
        status: str,
        source_counts: Dict[str, int],
        batch_count: int,
        pushed: bool,
        duration_seconds: float,
        adapter_states: Optional[Dict[str, Dict[str, Any]]] = None,
        matching: Optional[Dict[str, Any]] = None,
        fetch_errors: Optional[List[str]] = None,
        error: Optional[str] = None,
        payload: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        captured_at = datetime.utcnow().isoformat()
        entry: Dict[str, Any] = {
            "captured_at": captured_at,
            "market_name": market_name,
            "status": status,
            "source_counts": dict(source_counts),
            "batch_count": int(batch_count),
            "pushed": bool(pushed),
            "duration_seconds": round(float(duration_seconds), 3),
            "adapter_states": dict(adapter_states or {}),
            "matching": dict(matching or {}),
            "fetch_errors": list(fetch_errors or []),
            "error": str(error)[:200] if error else None,
        }
        if payload is not None:
            payload_json = json.dumps(payload, sort_keys=True)
            sample = payload[: self.payload_sample_size]
            entry.update(
                {
                    "payload_count": len(payload),
                    "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    "payload_sample": sample,
                    "payload_truncated": len(payload) > len(sample),
                }
            )
        self._history.append(entry)
        self._summary.update(
            {
                "last_cycle_at": captured_at,
                "last_push_at": captured_at if pushed else self._summary.get("last_push_at"),
                "last_status": status,
                "last_error": str(error)[:200] if error else None,
            }
        )
        self._save()
        return entry

    def snapshot_status(self) -> Dict[str, Any]:
        latest = self._history[-1] if self._history else None
        return {
            "state_path": self.state_path,
            "history_size": len(self._history),
            "history_limit": self.history_limit,
            **self._summary,
            "latest": latest,
        }

    def history(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        items = list(self._history)
        if limit is not None:
            items = items[-max(0, int(limit)) :]
        items.reverse()
        return items