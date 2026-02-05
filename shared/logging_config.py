"""
Centralized logging configuration for ArbDesk services.

Provides structured JSON logging for production and human-readable logs for development.
Logs are written to both console and rotating files.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, Optional


# Log directory - mounted as volume in Docker
LOG_DIR = Path(os.getenv("LOG_DIR", "/var/log/arb-desk"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")  # "json" or "text"
MAX_LOG_SIZE = int(os.getenv("MAX_LOG_SIZE_MB", "50")) * 1024 * 1024  # 50MB default
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))


class JSONFormatter(logging.Formatter):
    """JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: Dict[str, Any] = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "service": getattr(record, "service", os.getenv("SERVICE_NAME", "unknown")),
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extra fields
        if hasattr(record, "event_type"):
            log_data["event_type"] = record.event_type
        if hasattr(record, "bookmaker"):
            log_data["bookmaker"] = record.bookmaker
        if hasattr(record, "duration_ms"):
            log_data["duration_ms"] = record.duration_ms
        if hasattr(record, "url"):
            log_data["url"] = record.url
        if hasattr(record, "status_code"):
            log_data["status_code"] = record.status_code
        if hasattr(record, "odds_count"):
            log_data["odds_count"] = record.odds_count
        if hasattr(record, "error_type"):
            log_data["error_type"] = record.error_type

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data)


class ColoredFormatter(logging.Formatter):
    """Colored console formatter for development."""

    COLORS = {
        "DEBUG": "\033[36m",     # Cyan
        "INFO": "\033[32m",      # Green
        "WARNING": "\033[33m",   # Yellow
        "ERROR": "\033[31m",     # Red
        "CRITICAL": "\033[35m",  # Magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, self.RESET)
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        return super().format(record)


def setup_logging(service_name: str) -> logging.Logger:
    """
    Configure logging for a service.

    Args:
        service_name: Name of the service (e.g., "market_feed")

    Returns:
        Configured logger instance
    """
    # Create log directory if it doesn't exist
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, LOG_LEVEL))

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, LOG_LEVEL))

    if LOG_FORMAT == "json":
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(ColoredFormatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))

    root_logger.addHandler(console_handler)

    # File handler - rotating logs
    log_file = LOG_DIR / f"{service_name}.log"
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_SIZE,
        backupCount=LOG_BACKUP_COUNT,
    )
    file_handler.setLevel(logging.DEBUG)  # Always log everything to file
    file_handler.setFormatter(JSONFormatter())
    root_logger.addHandler(file_handler)

    # Browser-specific log file for market_feed
    if service_name == "market_feed":
        browser_log = LOG_DIR / "browser.log"
        browser_handler = RotatingFileHandler(
            browser_log,
            maxBytes=MAX_LOG_SIZE,
            backupCount=LOG_BACKUP_COUNT,
        )
        browser_handler.setLevel(logging.DEBUG)
        browser_handler.setFormatter(JSONFormatter())
        browser_handler.addFilter(lambda r: "browser" in r.name or "stealth" in r.name)
        root_logger.addHandler(browser_handler)

    # Create service-specific logger
    logger = logging.getLogger(service_name)
    logger.service = service_name

    logger.info(f"Logging initialized", extra={
        "event_type": "logging_init",
        "log_level": LOG_LEVEL,
        "log_format": LOG_FORMAT,
        "log_dir": str(LOG_DIR),
    })

    return logger

