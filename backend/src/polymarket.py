from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .models import OrderbookLevel, OrderbookSnapshot, PolymarketMeta

logger = logging.getLogger("moneymaker.polymarket")

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"


class PolymarketClient:
    """Async client for Polymarket Gamma (metadata) and CLOB (orderbook) APIs.

    Gamma API is fully public — no auth required for reads.
    CLOB API book endpoint is also public. Order placement requires
    API key + secret + passphrase (handled separately in execution layer).
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout

    async def get_market_by_slug(self, slug: str) -> PolymarketMeta:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{GAMMA_BASE}/markets", params={"slug": slug})
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                raise ValueError(f"No market found for slug: {slug}")
            return self._parse_market(markets[0])

    async def get_market_by_condition(self, condition_id: str) -> PolymarketMeta:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{GAMMA_BASE}/markets",
                params={"condition_id": condition_id},
            )
            resp.raise_for_status()
            markets = resp.json()
            if not markets:
                raise ValueError(f"No market found for condition_id: {condition_id}")
            return self._parse_market(markets[0])

    async def search_markets(self, query: str, limit: int = 10) -> list[PolymarketMeta]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{GAMMA_BASE}/markets",
                params={"closed": "false", "limit": limit},
            )
            resp.raise_for_status()
            markets = resp.json()
            results = []
            for m in markets:
                meta = self._parse_market(m)
                if query.lower() in meta.question.lower():
                    results.append(meta)
            return results

    async def get_orderbook(self, token_id: str) -> OrderbookSnapshot:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(
                f"{CLOB_BASE}/book",
                params={"token_id": token_id},
            )
            if resp.status_code != 200:
                logger.warning(
                    "CLOB book request failed (%d): %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return OrderbookSnapshot(token_id=token_id)

            data = resp.json()
            bids = [
                OrderbookLevel(price=float(b["price"]), size=float(b["size"]))
                for b in data.get("bids", [])
            ]
            asks = [
                OrderbookLevel(price=float(a["price"]), size=float(a["size"]))
                for a in data.get("asks", [])
            ]

            best_bid = bids[0].price if bids else 0.0
            best_ask = asks[0].price if asks else 1.0
            spread = best_ask - best_bid if best_bid and best_ask else 0.0

            return OrderbookSnapshot(
                token_id=token_id,
                bids=bids,
                asks=asks,
                spread=round(spread, 6),
                best_bid=best_bid,
                best_ask=best_ask,
            )

    async def get_orderbooks_for_market(
        self, meta: PolymarketMeta
    ) -> dict[str, OrderbookSnapshot]:
        """Fetch orderbooks for both YES and NO tokens of a market."""
        result: dict[str, OrderbookSnapshot] = {}
        if meta.yes_token_id:
            result["YES"] = await self.get_orderbook(meta.yes_token_id)
        if meta.no_token_id:
            result["NO"] = await self.get_orderbook(meta.no_token_id)
        return result

    @staticmethod
    def _parse_market(raw: dict) -> PolymarketMeta:
        clob_ids = raw.get("clobTokenIds") or []
        tokens = raw.get("tokens") or []

        yes_token = raw.get("yesTokenId", "")
        no_token = raw.get("noTokenId", "")

        if not yes_token and len(clob_ids) >= 1:
            yes_token = clob_ids[0]
        if not no_token and len(clob_ids) >= 2:
            no_token = clob_ids[1]

        if not yes_token and len(tokens) >= 1:
            yes_token = tokens[0].get("token_id", "")
        if not no_token and len(tokens) >= 2:
            no_token = tokens[1].get("token_id", "")

        return PolymarketMeta(
            condition_id=raw.get("conditionId", raw.get("condition_id", "")),
            question=raw.get("question", ""),
            description=raw.get("description", ""),
            slug=raw.get("slug", ""),
            yes_token_id=yes_token,
            no_token_id=no_token,
            end_date=raw.get("endDate", raw.get("end_date_iso", "")),
            active=raw.get("active", True),
            closed=raw.get("closed", False),
            volume_24h=float(raw.get("volume24hr", 0)),
            liquidity=float(raw.get("liquidity", 0)),
        )
