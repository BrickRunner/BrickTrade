"""
Mock классы бирж для тестирования без реальных API
"""
import asyncio
import random
from typing import Dict, Any, Optional, Callable

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("mock_exchanges")


class MockOKXWebSocket:
    """Mock WebSocket для OKX"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.testnet = testnet
        self.running = False
        self.callback: Optional[Callable] = None
        self.base_price = 50000.0  # Базовая цена BTC

    async def connect(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Симуляция подключения и отправки данных"""
        self.callback = callback
        self.running = True
        logger.info(f"[MOCK] OKX WebSocket connected for {self.symbol}")

        while self.running:
            try:
                price_variation = random.uniform(-50, 50)
                mid_price = self.base_price + price_variation

                spread = random.uniform(0.5, 2.0)

                orderbook = {
                    "exchange": "okx",
                    "symbol": self.symbol,
                    "bids": [
                        [mid_price - spread/2, random.uniform(0.1, 1.0)],
                        [mid_price - spread/2 - 1, random.uniform(0.1, 1.0)],
                        [mid_price - spread/2 - 2, random.uniform(0.1, 1.0)],
                    ],
                    "asks": [
                        [mid_price + spread/2, random.uniform(0.1, 1.0)],
                        [mid_price + spread/2 + 1, random.uniform(0.1, 1.0)],
                        [mid_price + spread/2 + 2, random.uniform(0.1, 1.0)],
                    ],
                    "timestamp": int(asyncio.get_event_loop().time() * 1000)
                }

                if self.callback:
                    await self.callback(orderbook)

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[MOCK] OKX error: {e}")
                await asyncio.sleep(1)

    async def disconnect(self) -> None:
        logger.info("[MOCK] Disconnecting OKX WebSocket")
        self.running = False

    def is_connected(self) -> bool:
        return self.running


class MockHTXWebSocket:
    """Mock WebSocket для HTX"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.testnet = testnet
        self.running = False
        self.callback: Optional[Callable] = None
        self.base_price = 50000.0

    async def connect(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Симуляция подключения и отправки данных"""
        self.callback = callback
        self.running = True
        logger.info(f"[MOCK] HTX WebSocket connected for {self.symbol}")

        while self.running:
            try:
                price_variation = random.uniform(-50, 50)
                mid_price = self.base_price + price_variation

                # Иногда создаём арбитражную возможность
                if random.random() < 0.3:
                    arbitrage_offset = random.uniform(10, 20)
                else:
                    arbitrage_offset = random.uniform(-5, 5)

                mid_price += arbitrage_offset
                spread = random.uniform(0.5, 2.0)

                orderbook = {
                    "exchange": "htx",
                    "symbol": self.symbol,
                    "bids": [
                        [mid_price - spread/2, random.uniform(0.1, 1.0)],
                        [mid_price - spread/2 - 1, random.uniform(0.1, 1.0)],
                        [mid_price - spread/2 - 2, random.uniform(0.1, 1.0)],
                    ],
                    "asks": [
                        [mid_price + spread/2, random.uniform(0.1, 1.0)],
                        [mid_price + spread/2 + 1, random.uniform(0.1, 1.0)],
                        [mid_price + spread/2 + 2, random.uniform(0.1, 1.0)],
                    ],
                    "timestamp": int(asyncio.get_event_loop().time() * 1000)
                }

                if self.callback:
                    await self.callback(orderbook)

                await asyncio.sleep(0.5)

            except Exception as e:
                logger.error(f"[MOCK] HTX error: {e}")
                await asyncio.sleep(1)

    async def disconnect(self) -> None:
        logger.info("[MOCK] Disconnecting HTX WebSocket")
        self.running = False

    def is_connected(self) -> bool:
        return self.running


class MockOKXRestClient:
    """Mock REST API для OKX"""

    def __init__(self, config, success_rate=0.95):
        self.config = config
        self.balance = 10000.0
        self.positions = []
        self.success_rate = success_rate  # Вероятность успеха ордера (0.0-1.0)
        logger.info("[MOCK] OKX REST client initialized")

        self.mock_instruments = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
            "ADAUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT", "AVAXUSDT",
            "LINKUSDT", "UNIUSDT", "LTCUSDT", "ATOMUSDT", "ETCUSDT"
        ]

    async def get_instruments(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX get_instruments: {len(self.mock_instruments)} instruments")
        data = []
        for symbol in self.mock_instruments:
            base = symbol.replace("USDT", "")
            data.append({
                "instId": f"{base}-USDT-SWAP",
                "instType": "SWAP",
                "ctVal": "0.01",
                "minSz": "1"
            })
        return {"code": "0", "data": data}

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX get_tickers: {len(self.mock_instruments)} tickers")
        data = []
        base_prices = {
            "BTCUSDT": 50000, "ETHUSDT": 3000, "BNBUSDT": 300, "SOLUSDT": 100,
            "XRPUSDT": 0.5, "ADAUSDT": 0.4, "DOGEUSDT": 0.08, "MATICUSDT": 0.9,
            "DOTUSDT": 7, "AVAXUSDT": 35, "LINKUSDT": 15, "UNIUSDT": 6,
            "LTCUSDT": 70, "ATOMUSDT": 10, "ETCUSDT": 20
        }
        for symbol in self.mock_instruments:
            base_price = base_prices.get(symbol, 100)
            price_var = random.uniform(-0.02, 0.02) * base_price
            mid_price = base_price + price_var
            spread = base_price * 0.0001
            base = symbol.replace("USDT", "")
            data.append({
                "instId": f"{base}-USDT-SWAP",
                "bidPx": str(mid_price - spread / 2),
                "askPx": str(mid_price + spread / 2),
                "vol24h": str(random.uniform(1000000, 10000000))
            })
        return {"code": "0", "data": data}

    async def get_balance(self) -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX get_balance: {self.balance} USDT")
        return {
            "code": "0",
            "data": [{"details": [{"ccy": "USDT", "availBal": str(self.balance)}]}]
        }

    async def get_positions(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX get_positions: {len(self.positions)} positions")
        return {"code": "0", "data": self.positions}

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "cross") -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX set_leverage: {leverage}x")
        return {"code": "0", "msg": "Success"}

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: Optional[float] = None,
        time_in_force: str = "ioc"
    ) -> Dict[str, Any]:
        order_id = f"mock_okx_{int(asyncio.get_event_loop().time() * 1000)}"
        if random.random() < self.success_rate:
            logger.info(f"[MOCK] OKX place_order SUCCESS: {side} {size} @ {price}, order_id={order_id}")
            if side == "buy":
                self.balance -= size * (price or 50000)
            else:
                self.balance += size * (price or 50000)
            return {"code": "0", "data": [{"ordId": order_id, "sCode": "0", "sMsg": "Order placed"}]}
        else:
            logger.warning(f"[MOCK] OKX place_order FAILED: {side} {size}")
            return {"code": "1", "msg": "Insufficient balance"}

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX cancel_order: {order_id}")
        return {"code": "0"}

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX get_order: {order_id}")
        return {"code": "0", "data": [{"ordId": order_id, "state": "filled"}]}

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        logger.info(f"[MOCK] OKX close_position: {side} {size}")
        return await self.place_order(symbol, "sell" if side == "long" else "buy", size, "market")

    async def close(self) -> None:
        logger.info("[MOCK] OKX REST session closed")


class MockHTXRestClient:
    """Mock REST API для HTX"""

    def __init__(self, config, success_rate=0.95):
        self.config = config
        self.balance = 10000.0
        self.positions = []
        self.success_rate = success_rate  # Вероятность успеха ордера (0.0-1.0)
        logger.info("[MOCK] HTX REST client initialized")

        self.mock_instruments = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
            "ADAUSDT", "DOGEUSDT", "MATICUSDT", "DOTUSDT", "AVAXUSDT",
            "LINKUSDT", "UNIUSDT", "LTCUSDT", "ATOMUSDT", "ETCUSDT"
        ]

    async def get_instruments(self) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX get_instruments: {len(self.mock_instruments)} instruments")
        data = []
        for symbol in self.mock_instruments:
            base = symbol.replace("USDT", "")
            data.append({
                "contract_code": f"{base}-USDT",
                "contract_type": "swap",
                "status": 1,
            })
        return {"status": "ok", "data": data}

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX get_tickers: {len(self.mock_instruments)} tickers")
        data = []
        base_prices = {
            "BTCUSDT": 50000, "ETHUSDT": 3000, "BNBUSDT": 300, "SOLUSDT": 100,
            "XRPUSDT": 0.5, "ADAUSDT": 0.4, "DOGEUSDT": 0.08, "MATICUSDT": 0.9,
            "DOTUSDT": 7, "AVAXUSDT": 35, "LINKUSDT": 15, "UNIUSDT": 6,
            "LTCUSDT": 70, "ATOMUSDT": 10, "ETCUSDT": 20
        }
        for symbol in self.mock_instruments:
            base_price = base_prices.get(symbol, 100)
            price_var = random.uniform(-0.02, 0.02) * base_price
            mid_price = base_price + price_var

            # Создаём арбитражные возможности для некоторых пар
            if random.random() < 0.6:
                arbitrage_offset = random.uniform(0.08, 0.15) * base_price
                mid_price += arbitrage_offset if random.random() < 0.5 else -arbitrage_offset

            spread = base_price * 0.0001
            base = symbol.replace("USDT", "")
            data.append({
                "contract_code": f"{base}-USDT",
                "bid": [mid_price - spread / 2, 10.0],
                "ask": [mid_price + spread / 2, 10.0],
                "amount": str(random.uniform(1000000, 10000000))
            })
        return {"status": "ok", "data": data}

    async def get_spot_tickers(self) -> Dict[str, Any]:
        return {"status": "ok", "data": []}

    async def get_funding_rates(self) -> Dict[str, Any]:
        data = []
        for symbol in self.mock_instruments:
            base = symbol.replace("USDT", "")
            data.append({
                "contract_code": f"{base}-USDT",
                "funding_rate": str(random.uniform(-0.001, 0.001))
            })
        return {"status": "ok", "data": data}

    async def get_balance(self) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX get_balance: {self.balance} USDT")
        return {
            "status": "ok",
            "data": [{"margin_asset": "USDT", "margin_available": self.balance}]
        }

    async def get_positions(self) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX get_positions: {len(self.positions)} positions")
        return {"status": "ok", "data": self.positions}

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX set_leverage: {leverage}x")
        return {"status": "ok", "data": {"lever_rate": leverage}}

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: float = 0.0,
        time_in_force: str = "",
        offset: str = "open",
        lever_rate: int = 3,
    ) -> Dict[str, Any]:
        order_id = int(asyncio.get_event_loop().time() * 1000)
        if random.random() < self.success_rate:
            logger.info(f"[MOCK] HTX place_order SUCCESS: {side} {size} @ {price}, order_id={order_id}")
            if side == "buy":
                self.balance -= size * (price or 50000)
            else:
                self.balance += size * (price or 50000)
            return {"status": "ok", "data": {"order_id": order_id}}
        else:
            logger.warning(f"[MOCK] HTX place_order FAILED: {side} {size}")
            return {"status": "error", "err-msg": "Insufficient balance"}

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX cancel_order: {order_id}")
        return {"status": "ok"}

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX get_order: {order_id}")
        return {"status": "ok", "data": [{"order_id": int(order_id), "status": 6}]}  # 6 = filled

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        logger.info(f"[MOCK] HTX close_position: {side} {size}")
        return await self.place_order(symbol, "sell" if side == "buy" else "buy", size, "opponent", offset="close")

    async def get_orderbook(self, symbol: str, category: str = "linear", limit: int = 5) -> Dict[str, Any]:
        base_price = 50000.0
        spread = 0.5
        data = {
            "bids": [[base_price - spread/2, 10.0], [base_price - spread - 1, 8.0]],
            "asks": [[base_price + spread/2, 10.0], [base_price + spread + 1, 8.0]],
            "ts": int(asyncio.get_event_loop().time() * 1000)
        }
        return {"status": "ok", "tick": data}

    async def close(self) -> None:
        logger.info("[MOCK] HTX REST session closed")


# Backward-compatible aliases (на случай если где-то ещё остались старые имена)
MockBybitWebSocket = MockHTXWebSocket
MockBybitRestClient = MockHTXRestClient
