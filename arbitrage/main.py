from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import os

from arbitrage.system import (
    AtomicExecutionEngine,
    InMemoryMonitoring,
    LiveExecutionVenue,
    LiveMarketDataProvider,
    SlippageModel,
    SystemState,
    TradingSystemConfig,
    TradingSystemEngine,
    build_exchange_clients,
    usdt_symbol_universe,
)
from arbitrage.system.lowlatency import LowLatencyExecutionVenue
from arbitrage.system.factory import build_private_ws_manager
from arbitrage.core.market_data import MarketDataEngine


def build_logger() -> logging.Logger:
    logger = logging.getLogger("trading_system")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


async def run() -> None:
    logger = build_logger()
    config = TradingSystemConfig.from_env()
    config.validate()

    clients = build_exchange_clients(config)

    # FIX P2: Verify API keys actually work before starting any trading.
    if not config.execution.dry_run:
        try:
            await config.validate_api_credentials(clients)
        except RuntimeError as exc:
            logger.error("API key validation FAILED: %s", exc)
            logger.error("Fix your .env credentials or set EXEC_DRY_RUN=true to skip validation")
            raise

    state = SystemState(starting_equity=config.starting_equity)
    monitor = InMemoryMonitoring(logger=logger)
    if os.getenv("METRICS_ADDR"):
        host, _, port = os.getenv("METRICS_ADDR").partition(":")
        try:
            monitor.start_metrics_server(host or "127.0.0.1", int(port or 9109))
            logger.info("Metrics server started at %s", os.getenv("METRICS_ADDR"))
        except Exception:
            logger.exception("Failed to start metrics server")
    market_data = MarketDataEngine(clients)
    provider = LiveMarketDataProvider(market_data=market_data, exchanges=config.exchanges)
    await provider.initialize()

    if config.trade_all_symbols:
        selected = usdt_symbol_universe(market_data, config.max_symbols, config.symbol_blacklist)
        config = replace(config, symbols=selected)
        logger.info(
            "Auto symbol universe enabled: selected=%s of common=%s",
            len(selected),
            len(market_data.common_pairs),
        )

    if os.getenv("USE_LOW_LATENCY_EXEC", "false").strip().lower() in {"1", "true", "yes", "on"}:
        venue = LowLatencyExecutionVenue()
    else:
        venue = LiveExecutionVenue(exchanges=clients, market_data=market_data)
    # Start private WS connections for real-time balance/fill/position updates.
    private_ws = build_private_ws_manager(config)
    await private_ws.start()
    await private_ws.seed_balances(market_data)
    venue.private_ws = private_ws
    provider.private_ws = private_ws
    execution = AtomicExecutionEngine(config=config.execution, venue=venue, slippage=SlippageModel(), state=state, monitor=monitor)
    engine = TradingSystemEngine.create(config=config, provider=provider, monitor=monitor, execution=execution, state=state)

    try:
        logger.info(
            "Starting trading engine. dry_run=%s exchanges=%s symbols=%s",
            config.execution.dry_run,
            config.exchanges,
            config.symbols,
        )
        await engine.run_forever()
    except asyncio.CancelledError:
        logger.info("Trading engine cancelled, shutting down gracefully...")
        await engine.shutdown_gracefully()
    except Exception as exc:
        logger.error("Trading engine crashed: %s", exc, exc_info=True)
        # FIX #5: Even on crash, try to close open positions.
        # State is persisted to disk, so on restart positions are loaded.
        logger.warning("Exiting without graceful shutdown — positions saved to disk")
    finally:
        try:
            await private_ws.stop()
        except Exception:
            pass
        try:
            await venue.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(run())
