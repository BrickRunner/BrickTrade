from __future__ import annotations

from typing import Any, Dict

from market_intelligence.service import get_market_intelligence_service


async def send_market_report(bot, user_id: int, force_refresh: bool = False) -> None:
    service = get_market_intelligence_service()
    await service.send_report(bot, user_id, force_refresh=force_refresh)


async def is_market_startup_enabled() -> bool:
    service = get_market_intelligence_service()
    try:
        return await service.is_startup_enabled()
    except Exception:
        return False


async def is_market_hourly_enabled() -> bool:
    service = get_market_intelligence_service()
    try:
        return await service.is_hourly_enabled()
    except Exception:
        return False


async def market_intelligence_health() -> Dict[str, Any]:
    service = get_market_intelligence_service()
    return await service.health_check()


async def shutdown_market_intelligence() -> None:
    service = get_market_intelligence_service()
    await service.close()
