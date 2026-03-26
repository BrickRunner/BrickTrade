"""
Lightweight HTTP healthcheck server for uptime monitoring.

Runs alongside the main bot. External services (UptimeRobot, Healthchecks.io)
can poll GET /health to verify the bot is alive.

Usage:
    # Start in background (called from main.py or standalone):
    from healthcheck import start_healthcheck_server
    await start_healthcheck_server()

    # Or run standalone:
    python healthcheck.py

Endpoints:
    GET /health  → 200 {"status": "ok", ...}
    GET /        → 200 {"status": "ok", ...}  (alias)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from aiohttp import web

logger = logging.getLogger("trading_system")

_HOST = os.getenv("HEALTHCHECK_HOST", "0.0.0.0")
_PORT = int(os.getenv("HEALTHCHECK_PORT", "8080"))
_POSITIONS_FILE = Path(os.getenv("POSITIONS_FILE", "data/open_positions.json"))

_start_time: float = time.time()
_runner: web.AppRunner | None = None


def _uptime_seconds() -> float:
    return time.time() - _start_time


def _load_position_count() -> int:
    try:
        if _POSITIONS_FILE.exists():
            with open(_POSITIONS_FILE, "r") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else 0
    except (json.JSONDecodeError, OSError):
        pass
    return 0


async def _health_handler(request: web.Request) -> web.Response:
    uptime = _uptime_seconds()
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)

    body = {
        "status": "ok",
        "uptime_seconds": round(uptime, 1),
        "uptime_human": f"{hours}h {minutes}m",
        "open_positions": _load_position_count(),
        "timestamp": time.time(),
    }
    return web.json_response(body)


def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", _health_handler)
    app.router.add_get("/", _health_handler)
    return app


async def start_healthcheck_server(
    host: str | None = None, port: int | None = None
) -> web.AppRunner:
    """Start the healthcheck HTTP server. Returns the runner for cleanup."""
    global _runner, _start_time
    _start_time = time.time()

    app = _build_app()
    _runner = web.AppRunner(app)
    await _runner.setup()

    h = host or _HOST
    p = port or _PORT
    site = web.TCPSite(_runner, h, p)
    await site.start()
    logger.info("Healthcheck server started on http://%s:%d/health", h, p)
    return _runner


async def stop_healthcheck_server() -> None:
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
        logger.info("Healthcheck server stopped")


if __name__ == "__main__":
    async def _main():
        runner = await start_healthcheck_server()
        print(f"Healthcheck running on http://{_HOST}:{_PORT}/health")
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    asyncio.run(_main())
