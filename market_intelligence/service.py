from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import Any, Dict, Optional

from aiogram import Bot

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.system.config import TradingSystemConfig
from arbitrage.system.factory import build_exchange_clients, usdt_symbol_universe
from market_intelligence.collector import MarketDataCollector
from market_intelligence.config import MarketIntelligenceConfig
from market_intelligence.engine import MarketIntelligenceEngine
from market_intelligence.output import format_human_report, format_json_report

logger = logging.getLogger("market_intelligence")


class MarketIntelligenceService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._engine: Optional[MarketIntelligenceEngine] = None
        self._collector: Optional[MarketDataCollector] = None
        self._market_data: Optional[MarketDataEngine] = None
        self._clients: Optional[dict] = None
        self._cfg: Optional[MarketIntelligenceConfig] = None
        self._last_report_ts: float = 0.0
        self._last_report = None

    async def initialize(self) -> None:
        if self._engine:
            return
        async with self._lock:
            if self._engine:
                return

            mi_cfg = MarketIntelligenceConfig.from_env()
            if not mi_cfg.enabled:
                raise RuntimeError("Market intelligence disabled")

            sys_cfg = TradingSystemConfig.from_env()
            sys_cfg.validate()

            if mi_cfg.exchanges:
                from dataclasses import replace as _replace
                sys_cfg = _replace(sys_cfg, exchanges=mi_cfg.exchanges)

            clients = build_exchange_clients(sys_cfg, validate_credentials=False)

            market_data = MarketDataEngine(clients)
            collector = MarketDataCollector(market_data=market_data, exchanges=list(clients.keys()), maxlen=mi_cfg.historical_window)
            await collector.initialize()

            symbols = mi_cfg.symbols
            if not symbols:
                symbols = usdt_symbol_universe(market_data, mi_cfg.max_symbols)
            mi_cfg = replace(mi_cfg, symbols=symbols, exchanges=list(clients.keys()))

            # BLOCK 2: Initialize structured logging if enabled
            if mi_cfg.structured_logging:
                from market_intelligence.structured_log import setup_structured_logging
                setup_structured_logging(log_dir=mi_cfg.log_dir)
                logger.info("Structured logging enabled, writing to %s/mi_structured.jsonl", mi_cfg.log_dir)

            engine = MarketIntelligenceEngine(mi_cfg, collector)
            self._engine = engine
            self._collector = collector
            self._market_data = market_data
            self._clients = clients
            self._cfg = mi_cfg
            logger.info("Market intelligence initialized exchanges=%s symbols=%s", mi_cfg.exchanges, len(mi_cfg.symbols))

    async def get_report(self, force_refresh: bool = False):
        await self.initialize()
        assert self._cfg and self._engine

        now = time.time()
        if not force_refresh and self._last_report and now - self._last_report_ts < self._cfg.interval_seconds:
            return self._last_report

        report = await self._engine.run_once()
        self._last_report = report
        self._last_report_ts = now
        return report

    async def send_report(self, bot: Bot, user_id: int, force_refresh: bool = False) -> None:
        report = await self.get_report(force_refresh=force_refresh)
        text = format_human_report(report)
        await self._send_chunked(bot, user_id, text, parse_mode="HTML")

    async def send_json_report(self, bot: Bot, user_id: int, force_refresh: bool = False) -> None:
        report = await self.get_report(force_refresh=force_refresh)
        js = format_json_report(report)
        await self._send_chunked(bot, user_id, f"<code>{js}</code>", parse_mode="HTML")

    async def close(self) -> None:
        if self._clients:
            for c in self._clients.values():
                if hasattr(c, "close"):
                    try:
                        await c.close()
                    except Exception:
                        pass
        self._engine = None
        self._collector = None
        self._market_data = None
        self._clients = None
        self._cfg = None
        self._last_report = None
        self._last_report_ts = 0.0

    async def health_check(self) -> Dict[str, Any]:
        """Lightweight health check without running a full cycle."""
        result: Dict[str, Any] = {
            "initialized": self._engine is not None,
            "last_report_age_seconds": None,
            "last_report_status": None,
            "exchanges": [],
            "symbols_count": 0,
        }
        if self._cfg:
            result["exchanges"] = self._cfg.exchanges
            result["symbols_count"] = len(self._cfg.symbols)
        if self._last_report:
            result["last_report_age_seconds"] = time.time() - self._last_report_ts
            result["last_report_status"] = self._last_report.payload.get("status")
        if self._collector:
            breaker_status = {}
            for ex, cb in self._collector._circuit_breakers.items():
                breaker_status[ex] = {
                    "available": cb.is_available(),
                    "failure_count": cb.failure_count,
                }
            result["circuit_breakers"] = breaker_status
        return result

    async def is_startup_enabled(self) -> bool:
        try:
            cfg = MarketIntelligenceConfig.from_env()
            return bool(cfg.enabled and cfg.startup_report_enabled)
        except Exception:
            return False

    async def is_hourly_enabled(self) -> bool:
        try:
            cfg = MarketIntelligenceConfig.from_env()
            return bool(cfg.enabled and cfg.hourly_report_enabled)
        except Exception:
            return False

    async def _send_chunked(self, bot: Bot, user_id: int, text: str, parse_mode: str = "HTML") -> None:
        # Telegram hard limit: 4096 chars. Keep conservative margin.
        limit = 3500
        if len(text) <= limit:
            await bot.send_message(user_id, text, parse_mode=parse_mode)
            return

        chunks = self._split_text(text, limit)
        for idx, chunk in enumerate(chunks):
            if idx == 0:
                await bot.send_message(user_id, chunk, parse_mode=parse_mode)
            else:
                await bot.send_message(user_id, f"<i>continued {idx+1}/{len(chunks)}</i>\n{chunk}", parse_mode=parse_mode)

    @staticmethod
    def _split_text(text: str, limit: int) -> list[str]:
        lines = text.split("\n")
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for line in lines:
            line_len = len(line) + 1
            if line_len > limit:
                # Fallback: hard-split very long line.
                if current:
                    chunks.append("\n".join(current))
                    current = []
                    current_len = 0
                start = 0
                while start < len(line):
                    part = line[start:start + limit]
                    chunks.append(part)
                    start += limit
                continue

            if current_len + line_len > limit and current:
                chunks.append("\n".join(current))
                current = [line]
                current_len = line_len
            else:
                current.append(line)
                current_len += line_len

        if current:
            chunks.append("\n".join(current))
        return chunks


_service: Optional[MarketIntelligenceService] = None


def get_market_intelligence_service() -> MarketIntelligenceService:
    global _service
    if _service is None:
        _service = MarketIntelligenceService()
    return _service
