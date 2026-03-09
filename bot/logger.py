"""Structured JSON logging for the arbitrage bot."""

import json
import logging
import re
import sys
from datetime import datetime, timezone

# Matches hex strings that look like private keys (64 hex chars, optional 0x prefix)
_KEY_PATTERN = re.compile(r'0x[0-9a-fA-F]{64}')


def _scrub_secrets(text: str) -> str:
    """Redact anything that looks like a private key."""
    return _KEY_PATTERN.sub('[REDACTED]', text)


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines."""

    def format(self, record):
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": _scrub_secrets(record.getMessage()),
        }
        # Include extra fields if present
        for key in ("event", "market_id", "token_id", "price", "size",
                     "category", "order_id", "tx_hash", "details"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[0]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry)


class ConsoleFormatter(logging.Formatter):
    """Human-readable format for console output."""

    def format(self, record):
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        event = getattr(record, "event", "")
        prefix = f"[{event}] " if event else ""
        msg = f"{ts} {record.levelname:<5} {prefix}{_scrub_secrets(record.getMessage())}"
        if record.exc_info and record.exc_info[0]:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


def setup_logging(log_file: str = "bot.log", log_level: str = "INFO") -> logging.Logger:
    """Configure logging with console (human) + file (JSON) handlers."""
    logger = logging.getLogger("arb_bot")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    logger.handlers.clear()

    # Console handler — human-readable
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    # File handler — JSON lines
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    return logger


def log_event(logger: logging.Logger, event: str, message: str, level: str = "INFO", **kwargs):
    """Log a structured event with extra fields."""
    extra = {"event": event}
    extra.update(kwargs)
    log_func = getattr(logger, level.lower(), logger.info)
    log_func(message, extra=extra)
