from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from arbitrage.system.models import MarketSnapshot


@dataclass
class ReplayMarketDataProvider:
    frames_by_symbol: Dict[str, List[MarketSnapshot]]

    def __post_init__(self) -> None:
        self._idx: Dict[str, int] = {symbol: 0 for symbol in self.frames_by_symbol}

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        frames = self.frames_by_symbol[symbol]
        idx = self._idx[symbol]
        if idx >= len(frames):
            return frames[-1]
        self._idx[symbol] += 1
        return frames[idx]

    async def health(self) -> Dict[str, float]:
        return {"simulated": 1.0}
