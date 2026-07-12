from __future__ import annotations

import asyncio
import random
from datetime import datetime

from .models import Tick, MarketType


class FakeFeed:
    """Generates realistic-looking fake market ticks."""

    def __init__(self) -> None:
        self._base_prices: dict[str, float] = {
            "BTC/USDT": 67_500.0,
            "ETH/USDT": 3_420.0,
            "SOL/USDT": 148.0,
            "DOGE/USDT": 0.162,
            "TRUMP-WIN": 0.42,
            "BTC-100K-DEC": 0.18,
            "ETH-5K-JUN": 0.31,
        }
        self._markets: dict[str, MarketType] = {
            "BTC/USDT": MarketType.CRYPTO,
            "ETH/USDT": MarketType.CRYPTO,
            "SOL/USDT": MarketType.CRYPTO,
            "DOGE/USDT": MarketType.CRYPTO,
            "TRUMP-WIN": MarketType.POLYMARKET,
            "BTC-100K-DEC": MarketType.POLYMARKET,
            "ETH-5K-JUN": MarketType.POLYMARKET,
        }

    def _drift(self, symbol: str) -> float:
        base = self._base_prices[symbol]
        volatility = 0.002 if self._markets[symbol] == MarketType.CRYPTO else 0.005
        change = random.gauss(0, volatility)
        self._base_prices[symbol] = base * (1 + change)
        return self._base_prices[symbol]

    def generate_tick(self) -> Tick:
        symbol = random.choice(list(self._base_prices.keys()))
        price = self._drift(symbol)
        market = self._markets[symbol]
        volume = random.uniform(100_000, 5_000_000) if market == MarketType.CRYPTO else random.uniform(10_000, 500_000)
        change = random.uniform(-3.0, 3.0)

        extra = {}
        if market == MarketType.POLYMARKET:
            extra["probability"] = round(min(max(price, 0.01), 0.99), 4)
            extra["liquidity_usd"] = round(random.uniform(50_000, 2_000_000), 2)
            price = extra["probability"]

        return Tick(
            timestamp=datetime.utcnow(),
            market=market,
            symbol=symbol,
            price=round(price, 6),
            volume_24h=round(volume, 2),
            change_pct=round(change, 2),
            extra=extra,
        )

    async def stream(self, interval: float = 1.0):
        while True:
            yield self.generate_tick()
            await asyncio.sleep(interval)
