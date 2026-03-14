"""Structured logging adapter for Market Intelligence pipeline.

Zero external dependencies — wraps stdlib logging with JSON-structured
context propagation (cycle_id, symbol, stage).
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional


# Context variable for cycle-level correlation
_cycle_ctx: ContextVar[Dict[str, Any]] = ContextVar("mi_cycle_ctx", default={})


def new_cycle_context(cycle_number: int) -> Dict[str, Any]:
    """Create and set a new cycle context. Call at start of run_once()."""
    ctx = {
        "cycle_id": uuid.uuid4().hex[:12],
        "cycle_number": cycle_number,
        "started_at": time.time(),
    }
    _cycle_ctx.set(ctx)
    return ctx


def get_cycle_context() -> Dict[str, Any]:
    return _cycle_ctx.get()


class StructuredFormatter(logging.Formatter):
    """JSON-line formatter that injects cycle context."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = _cycle_ctx.get()
        entry: Dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if ctx:
            entry["cycle_id"] = ctx.get("cycle_id", "")
            entry["cycle_number"] = ctx.get("cycle_number", 0)
        # Merge extra structured fields
        if hasattr(record, "structured"):
            entry.update(record.structured)
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


class StructuredLogger:
    """Thin wrapper around stdlib logger with structured context."""

    def __init__(self, name: str) -> None:
        self._logger = logging.getLogger(name)

    def _log(self, level: int, msg: str, **fields: Any) -> None:
        if not self._logger.isEnabledFor(level):
            return
        record = self._logger.makeRecord(
            self._logger.name, level, "(structured)", 0, msg, (), None
        )
        record.structured = fields  # type: ignore[attr-defined]
        self._logger.handle(record)

    def debug(self, msg: str, **fields: Any) -> None:
        self._log(logging.DEBUG, msg, **fields)

    def info(self, msg: str, **fields: Any) -> None:
        self._log(logging.INFO, msg, **fields)

    def warning(self, msg: str, **fields: Any) -> None:
        self._log(logging.WARNING, msg, **fields)

    def error(self, msg: str, **fields: Any) -> None:
        self._log(logging.ERROR, msg, **fields)

    def critical(self, msg: str, **fields: Any) -> None:
        self._log(logging.CRITICAL, msg, **fields)


def get_structured_logger(name: str) -> StructuredLogger:
    return StructuredLogger(f"mi.{name}")


def setup_structured_logging(
    log_dir: str = "logs",
    level: int = logging.INFO,
    also_plain: bool = True,
) -> None:
    """Configure structured logging for MI pipeline.

    Args:
        log_dir: Directory for structured log file.
        level: Logging level.
        also_plain: If True, keep plain text handler for console.
    """
    import os
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger("mi")
    root.setLevel(level)

    # JSON file handler
    json_handler = logging.FileHandler(
        os.path.join(log_dir, "mi_structured.jsonl"), encoding="utf-8"
    )
    json_handler.setFormatter(StructuredFormatter())
    root.addHandler(json_handler)

    # Optional plain console handler
    if also_plain:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        ))
        root.addHandler(console)
