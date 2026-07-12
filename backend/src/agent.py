from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

from .models import AISignal, HarnessVerdict, PolymarketMeta, WSMessage
from .polymarket import PolymarketClient
from .validation import TradingHarness

logger = logging.getLogger("moneymaker.agent")

SYSTEM_PROMPT = """\
You are a disciplined quantitative trading agent analyzing prediction markets.

You operate in a HARNESS environment. Your ONLY output is a JSON signal.
The harness validates your output deterministically — if your JSON is malformed
or violates risk limits, the trade is BLOCKED. You cannot override this.

RULES:
1. Think step by step. Examine the market data, orderbook, and resolution rules.
2. Output a JSON object with EXACTLY these fields:
   - "market_type": "polymarket" | "crypto" | "stocks"
   - "asset_id": the condition_id or token_id
   - "outcome": "YES" | "NO" | "BUY" | "SELL"
   - "price_limit": your max entry price (0.01–0.99 for polymarket)
   - "size_pusd": position size in pUSD (CLOB V2 collateral)
   - "reasoning": your analysis in 1-3 sentences
3. Risk limits: max 500 pUSD per trade, max 5000 pUSD total exposure.
4. NEVER output anything except the raw JSON object. No markdown, no explanations outside the JSON.
5. If the data is insufficient for a confident trade, output:
   {"market_type":"polymarket","asset_id":"SKIP","outcome":"YES","price_limit":0.5,"size_pusd":0,"reasoning":"Insufficient data for confident entry"}
"""


class AgentConfig:
    def __init__(
        self,
        api_key: str = "",
        model: str = "openrouter/anthropic/claude-sonnet-4",
        base_url: str = "https://openrouter.ai/api/v1",
        agent_id: str = "agent-001",
        max_risk_per_trade: float = 200.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.model = model
        self.base_url = base_url
        self.agent_id = agent_id
        self.max_risk_per_trade = max_risk_per_trade


class AgentOrchestrator:
    """AI agent that researches Polymarket via Gamma+CLOB, reasons, and emits
    AISignal JSON through the TradingHarness."""

    def __init__(
        self,
        config: AgentConfig | None = None,
        harness: TradingHarness | None = None,
        polymarket: PolymarketClient | None = None,
        db=None,
    ) -> None:
        self._config = config or AgentConfig()
        self._harness = harness or TradingHarness()
        self._pm = polymarket or PolymarketClient()
        self._db = db
        self._running = False
        self._signals_emitted = 0

    @property
    def agent_id(self) -> str:
        return self._config.agent_id

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def signals_emitted(self) -> int:
        return self._signals_emitted

    async def run_cycle(
        self, market_query: str, target_token_id: str | None = None
    ) -> AsyncIterator[WSMessage]:
        """Execute one research→reason→signal cycle.

        Yields WSMessage objects for real-time streaming to the frontend:
        - type "log": agent reasoning steps
        - type "signal": final harness verdict
        """
        self._running = True

        # ── Step 1: Research market — check DuckDB cache first ──
        meta: PolymarketMeta | None = None

        if self._db:
            cached = self._db.get_cached_market_by_slug(market_query)
            if not cached:
                cached = self._db.get_cached_market(market_query)
            if cached:
                meta = PolymarketMeta(
                    condition_id=cached["condition_id"],
                    question=cached["title"],
                    description=cached["resolution_rules"] or "",
                    slug=cached["slug"] or "",
                    yes_token_id=cached["yes_token"] or "",
                    no_token_id=cached["no_token"] or "",
                )
                yield _log(f"Cache hit: {meta.question}")

        if meta is None:
            yield _log("Searching Gamma API for market data...")
            try:
                markets = await self._pm.search_markets(market_query, limit=5)
            except Exception as e:
                yield _log(f"Gamma API search failed: {e}", level="error")
                self._running = False
                return

            if not markets:
                yield _log(f"No markets found for '{market_query}'", level="warning")
                self._running = False
                return

            meta = markets[0]

            # Cache in DuckDB
            if self._db:
                self._db.cache_market(meta.model_dump(mode="json"))
                yield _log(f"Cached market metadata in DuckDB")

        yield _log(f"Market: {meta.question}")
        yield _log(f"Condition ID: {meta.condition_id}")
        yield _log(f"Resolution rules: {meta.description[:300]}...")

        # ── Step 2: Fetch orderbook via CLOB API ──
        yield _log("Fetching orderbook from CLOB API...")

        books: dict[str, Any] = {}
        try:
            raw_books = await self._pm.get_orderbooks_for_market(meta)
            for side, snapshot in raw_books.items():
                books[side] = {
                    "best_bid": snapshot.best_bid,
                    "best_ask": snapshot.best_ask,
                    "spread": snapshot.spread,
                    "bid_depth": len(snapshot.bids),
                    "ask_depth": len(snapshot.asks),
                    "top_bid": [{"price": b.price, "size": b.size} for b in snapshot.bids[:5]],
                    "top_ask": [{"price": a.price, "size": a.size} for a in snapshot.asks[:5]],
                }
            if books:
                yield _log(f"Orderbook YES: bid={books.get('YES', {}).get('best_bid', '?')} ask={books.get('YES', {}).get('best_ask', '?')}")
            else:
                yield _log("No orderbook data available", level="warning")
        except Exception as e:
            yield _log(f"CLOB orderbook fetch failed: {e}", level="error")

        # ── Step 3: Build context for LLM ──
        context = self._build_context(meta, books, market_query)
        yield _log("Context built. Calling LLM for analysis...")

        # ── Step 4: Call LLM ──
        raw_response = await self._call_llm(context)
        if raw_response is None:
            yield _log("LLM call failed or returned empty", level="error")
            self._running = False
            return

        yield _log(f"LLM response received ({len(raw_response)} chars)")

        # ── Step 5: Parse signal ──
        signal_data = self._parse_signal(raw_response)
        if signal_data is None:
            yield _log(f"Failed to parse signal from LLM output", level="error")
            yield _log(f"Raw output: {raw_response[:500]}")
            self._running = False
            return

        signal_data["agent_id"] = self.agent_id
        yield _log(f"Parsed signal: {signal_data.get('outcome')} @ {signal_data.get('price_limit')} ({signal_data.get('size_pusd')} pUSD)")

        # ── Step 6: Validate through Harness ──
        yield _log("Passing through TradingHarness...")

        verdict = self._harness.verify_and_route(signal_data)

        if verdict.status == "APPROVED":
            self._signals_emitted += 1
            yield _log(f"SIGNAL APPROVED → {verdict.route}")
        else:
            yield _log(f"SIGNAL {verdict.status}: {verdict.reason}", level="warning")

        yield WSMessage(
            type="signal",
            data={
                **verdict.model_dump(mode="json"),
                "market_context": {
                    "question": meta.question,
                    "condition_id": meta.condition_id,
                    "orderbooks": books,
                },
            },
        )

        self._running = False

    def _build_context(
        self, meta: PolymarketMeta, books: dict, query: str
    ) -> str:
        orderbook_text = ""
        for side, data in books.items():
            orderbook_text += (
                f"\n{side} Token Orderbook:\n"
                f"  Best Bid: {data.get('best_bid', 'N/A')}  "
                f"Best Ask: {data.get('best_ask', 'N/A')}  "
                f"Spread: {data.get('spread', 'N/A')}\n"
                f"  Bid depth: {data.get('bid_depth', 0)} levels  "
                f"Ask depth: {data.get('ask_depth', 0)} levels\n"
            )
            top_bids = data.get("top_bid", [])
            top_asks = data.get("top_ask", [])
            if top_bids:
                orderbook_text += f"  Top bids: {', '.join(f'{b['price']}({b['size']})' for b in top_bids)}\n"
            if top_asks:
                orderbook_text += f"  Top asks: {', '.join(f'{a['price']}({a['size']})' for a in top_asks)}\n"

        return f"""\
MARKET RESEARCH DATA
====================
Search query: {query}
Market: {meta.question}
Condition ID: {meta.condition_id}
Status: {'Active' if meta.active else 'Closed'}
Volume 24h: {meta.volume_24h}
Liquidity: {meta.liquidity}
End Date: {meta.end_date or 'Unknown'}

RESOLUTION RULES:
{meta.description or 'Not available'}
{orderbook_text}
INSTRUCTIONS:
Analyze this market. Consider the resolution rules, current prices, and orderbook depth.
Output a JSON signal object. If uncertain, output SKIP with size_pusd=0.
Remember: price_limit must be 0.01-0.99 for Polymarket.
Current time: {datetime.utcnow().isoformat()}Z"""

    async def _call_llm(self, context: str) -> str | None:
        if not self._config.api_key:
            logger.warning("No API key configured — LLM call skipped")
            return None

        headers = {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": context},
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{self._config.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return None

    @staticmethod
    def _parse_signal(raw: str) -> dict | None:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except json.JSONDecodeError:
                    pass
        return None


def _log(message: str, level: str = "info") -> WSMessage:
    return WSMessage(
        type="log",
        data={
            "timestamp": datetime.utcnow().isoformat(),
            "level": level,
            "source": "agent",
            "message": message,
        },
    )
