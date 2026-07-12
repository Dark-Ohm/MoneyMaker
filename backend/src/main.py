from __future__ import annotations

import asyncio
import json
import logging
import socket
import sys
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from .db import Database
from .feed import FakeFeed
from .models import WSMessage
from .polymarket import PolymarketClient
from .validation import TradingHarness, TradingHarnessConfig
from .agent import AgentConfig, AgentOrchestrator
from .crypto_vault import CryptoVault
from .executor import CLOBClient
from .rate_limiter import SlidingWindowRateLimiter, RateLimitConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("moneymaker")


# State container for thread-safe access to executor
class ExecutorState:
    """Thread-safe container for executor lifecycle management."""
    def __init__(self):
        self._lock = threading.Lock()
        self._executor: CLOBClient | None = None

    def get(self) -> CLOBClient | None:
        with self._lock:
            return self._executor

    def set(self, executor: CLOBClient | None) -> None:
        with self._lock:
            self._executor = executor

    def is_configured(self) -> bool:
        return self.get() is not None


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _find_free_port()
db = Database()
feed = FakeFeed()
vault = CryptoVault()
executor_state = ExecutorState()
rate_limiter = SlidingWindowRateLimiter(db._conn, RateLimitConfig(max_events=30, window_seconds=60))
harness = TradingHarness(TradingHarnessConfig(), executor=None, rate_limiter=rate_limiter)
polymarket = PolymarketClient()
agent = AgentOrchestrator(
    config=AgentConfig(),
    harness=harness,
    polymarket=polymarket,
    db=db,
)

connected_clients: set[WebSocket] = set()


async def broadcast(message: WSMessage) -> None:
    payload = message.model_dump_json()
    disconnected: list[WebSocket] = []
    for ws in connected_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        connected_clients.discard(ws)


async def tick_producer() -> None:
    logger.info("Tick producer started")
    async for tick in feed.stream(interval=1.0):
        db.insert_tick(tick.model_dump(mode="json"))
        msg = WSMessage(type="tick", data=tick.model_dump(mode="json"))
        await broadcast(msg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"MoneyMaker sidecar starting on port {PORT}")
    db.insert_log("info", "system", f"Sidecar started on port {PORT}")

    producer = asyncio.create_task(tick_producer())

    yield

    producer.cancel()
    db.close()
    logger.info("MoneyMaker sidecar stopped")


app = FastAPI(title="MoneyMaker Sidecar", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:*", "http://localhost:*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ─── Health & Status ────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "port": PORT, "uptime": datetime.utcnow().isoformat()}


@app.get("/api/status")
async def status() -> dict[str, Any]:
    return {
        "sidecar": "running",
        "port": PORT,
        "db_path": str(db._path),
        "connected_clients": len(connected_clients),
    }


# ─── Agent Control ──────────────────────────────────────────────────────────

@app.post("/api/agent/start")
async def start_agent(body: dict) -> dict[str, Any]:
    agent_id = body.get("agent_id", "agent-001")
    query = body.get("query", "president")
    token_id = body.get("token_id")

    logger.info(f"Starting agent cycle: {agent_id} → {query}")
    db.insert_log("info", "agent", f"Agent {agent_id} started for '{query}'")

    # Stream reasoning steps + final signal via WebSocket
    async def _run():
        async for msg in agent.run_cycle(market_query=query, target_token_id=token_id):
            await broadcast(msg)
        status_msg = WSMessage(
            type="status",
            data={
                "agent_id": agent_id,
                "running": False,
                "signals_emitted": agent.signals_emitted,
            },
        )
        await broadcast(status_msg)

    asyncio.create_task(_run())

    return {"agent_id": agent_id, "running": True, "query": query}


@app.post("/api/agent/stop")
async def stop_agent(body: dict) -> dict[str, Any]:
    agent_id = body.get("agent_id", "agent-001")
    logger.info(f"Stopping agent: {agent_id}")
    db.insert_log("info", "agent", f"Agent {agent_id} stopped")
    await broadcast(WSMessage(type="status", data={"agent_id": agent_id, "running": False}))
    return {"agent_id": agent_id, "running": False}


@app.get("/api/agent/status")
async def agent_status() -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id,
        "running": agent.is_running,
        "signals_emitted": agent.signals_emitted,
    }


# ─── Signal Validation (Trading Harness) ───────────────────────────────────

@app.post("/api/signal/validate")
async def validate_signal(body: dict) -> dict[str, Any]:
    verdict = harness.verify_and_route(body)
    db.insert_signal({
        "timestamp": datetime.utcnow(),
        "agent_id": body.get("agent_id", "default"),
        "symbol": body.get("asset_id", ""),
        "action": body.get("outcome", ""),
        "confidence": 0.0,
        "reasoning": verdict.reason,
        "metadata": verdict.model_dump(mode="json"),
    })
    await broadcast(WSMessage(type="signal", data=verdict.model_dump(mode="json")))
    return verdict.model_dump(mode="json")


@app.post("/api/signal/execute")
async def execute_signal(body: dict) -> dict[str, Any]:
    # Get current executor from state container
    current_executor = executor_state.get()
    harness._executor = current_executor
    verdict = harness.verify_and_route(body)
    if verdict.status == "APPROVED":
        verdict = await harness.execute_approved(verdict)
    db.insert_signal({
        "timestamp": datetime.utcnow(),
        "agent_id": body.get("agent_id", "default"),
        "symbol": body.get("asset_id", ""),
        "action": body.get("outcome", ""),
        "confidence": 0.0,
        "reasoning": verdict.reason,
        "metadata": verdict.model_dump(mode="json"),
    })
    await broadcast(WSMessage(type="signal", data=verdict.model_dump(mode="json")))
    return verdict.model_dump(mode="json")


# ─── Crypto Vault ──────────────────────────────────────────────────────────

@app.get("/api/vault/status")
async def vault_status() -> dict[str, Any]:
    return {
        "has_vault": vault.has_vault,
        "is_unlocked": vault.is_unlocked,
        "dry_run": harness._config.dry_run,
        "has_executor": executor_state.is_configured(),
    }


@app.post("/api/vault/unlock")
async def vault_unlock(body: dict) -> dict[str, Any]:
    password = body.get("password", "")
    if not password:
        return {"success": False, "error": "Password required"}

    ok = vault.unlock(password)
    if not ok:
        return {"success": False, "error": "Invalid master password"}

    # If vault has a stored key, decrypt and initialize executor
    pk = vault.decrypt_private_key()
    if pk:
        api_key = body.get("api_key", "")
        api_secret = body.get("api_secret", "")
        api_passphrase = body.get("api_passphrase", "")
        client = CLOBClient(
            private_key_hex=pk,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        executor_state.set(client)
        # Update harness executor reference
        harness._executor = client

    return {
        "success": True,
        "has_key": pk is not None,
        "dry_run": harness._config.dry_run,
    }


@app.post("/api/vault/lock")
async def vault_lock() -> dict[str, Any]:
    vault.lock()
    executor_state.set(None)
    harness._executor = None
    return {"success": True}


@app.post("/api/vault/import-key")
async def vault_import_key(body: dict) -> dict[str, Any]:
    if not vault.is_unlocked:
        return {"success": False, "error": "Vault is locked"}
    pk = body.get("private_key", "")
    if not pk:
        return {"success": False, "error": "Private key required"}
    vault.encrypt_private_key(pk)
    return {"success": True}


@app.post("/api/vault/set-dry-run")
async def vault_set_dry_run(body: dict) -> dict[str, Any]:
    dry_run = body.get("dry_run", True)
    harness._config.dry_run = dry_run
    return {"dry_run": dry_run}


@app.post("/api/vault/change-password")
async def vault_change_password(body: dict) -> dict[str, Any]:
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if not old_pw or not new_pw:
        return {"success": False, "error": "Both passwords required"}
    ok = vault.change_password(old_pw, new_pw)
    return {"success": ok}


# ─── News Ingestion Endpoints ────────────────────────────────────────────────

from .news_ingest import NewsIngestionService, get_market_context

news_service = NewsIngestionService(db)


@app.get("/api/news/crypto")
async def get_crypto_news() -> dict[str, Any]:
    """Get top coins, global metrics, and latest crypto news from CoinMarketCap."""
    return await news_service.fetch_crypto_snapshot()


@app.get("/api/news/macro")
async def get_macro_news(
    sections: str = "business,technology,world",
) -> dict[str, Any]:
    """Get macro news from BBC RSS feeds.
    sections: comma-separated list (business,technology,world,politics,health)"""
    section_list = [s.strip() for s in sections.split(",") if s.strip()]
    items = await news_service.fetch_macro_news(section_list)
    return {"items": [NewsIngestionService._item_to_dict(i) for i in items]}


@app.get("/api/news/regulatory")
async def get_regulatory_filings(
    queries: str = "merger agreement,acquisition,force majeure,going concern",
    forms: str = "8-K,10-K,10-Q,DEF 14A,13D,13G",
    days_back: int = 7,
) -> dict[str, Any]:
    """Search SEC EDGAR for market-moving filings."""
    query_list = [q.strip() for q in queries.split(",") if q.strip()]
    form_list = [f.strip() for f in forms.split(",") if f.strip()]
    results = await news_service.fetch_regulatory_filings(query_list, form_list, days_back)
    return {"batches": results}


@app.get("/api/news/all")
async def get_all_news() -> dict[str, Any]:
    """Get full market context: crypto snapshot + macro news + regulatory filings."""
    return await get_market_context(db)


# ─── Polymarket Endpoints ───────────────────────────────────────────────────

@app.get("/api/polymarket/market/{slug}")
async def get_polymarket_market(slug: str) -> dict[str, Any]:
    try:
        meta = await polymarket.get_market_by_slug(slug)
        return meta.model_dump(mode="json")
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/polymarket/market/by-condition/{condition_id}")
async def get_polymarket_by_condition(condition_id: str) -> dict[str, Any]:
    try:
        meta = await polymarket.get_market_by_condition(condition_id)
        return meta.model_dump(mode="json")
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/polymarket/search")
async def search_polymarket(q: str, limit: int = 10) -> dict[str, Any]:
    try:
        results = await polymarket.search_markets(q, limit=limit)
        return {"markets": [m.model_dump(mode="json") for m in results]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/polymarket/orderbook/{token_id}")
async def get_polymarket_orderbook(token_id: str) -> dict[str, Any]:
    try:
        snapshot = await polymarket.get_orderbook(token_id)
        return snapshot.model_dump(mode="json")
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/polymarket/orderbooks/{condition_id}")
async def get_polymarket_orderbooks(condition_id: str) -> dict[str, Any]:
    try:
        meta = await polymarket.get_market_by_condition(condition_id)
        books = await polymarket.get_orderbooks_for_market(meta)
        return {
            "market": meta.model_dump(mode="json"),
            "orderbooks": {k: v.model_dump(mode="json") for k, v in books.items()},
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/polymarket/signal")
async def polymarket_signal(body: dict) -> dict[str, Any]:
    verdict = harness.verify_and_route(body)
    if verdict.status == "APPROVED":
        meta = await polymarket.get_market_by_condition(body["asset_id"])
        books = await polymarket.get_orderbooks_for_market(meta)
        verdict.metadata["market"] = meta.model_dump(mode="json")
        verdict.metadata["orderbooks"] = {
            k: v.model_dump(mode="json") for k, v in books.items()
        }
    await broadcast(WSMessage(type="signal", data=verdict.model_dump(mode="json")))
    return verdict.model_dump(mode="json")


# ─── Chart Data Endpoints ──────────────────────────────────────────────────

@app.get("/api/chart/ticks")
async def chart_ticks(
    symbol: str | None = None,
    market: str | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Return historical ticks for chart rendering."""
    sql = "SELECT timestamp, market, symbol, price, volume_24h, change_pct FROM ticks"
    conditions: list[str] = []
    params: list[Any] = []
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if market:
        conditions.append("market = ?")
        params.append(market)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, params)
    rows.reverse()  # oldest first for chart
    return {"ticks": rows}


@app.get("/api/chart/signals")
async def chart_signals(
    symbol: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return historical signals for chart markers."""
    sql = "SELECT timestamp, agent_id, symbol, action, reasoning FROM signals"
    conditions: list[str] = []
    params: list[Any] = []
    if symbol:
        conditions.append("symbol = ?")
        params.append(symbol)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, params)
    return {"signals": rows}


# ─── WebSocket ──────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    connected_clients.add(ws)
    logger.info(f"WebSocket client connected ({len(connected_clients)} total)")
    db.insert_log("info", "ws", f"Client connected ({len(connected_clients)} total)")

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong", "ts": datetime.utcnow().isoformat()}))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        connected_clients.discard(ws)
        logger.info(f"WebSocket client disconnected ({len(connected_clients)} total)")


def main() -> None:
    logger.info("MoneyMaker Python Sidecar v0.1.0")
    logger.info(f"Port: {PORT}")

    print(str(PORT), flush=True)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info", access_log=False)


if __name__ == "__main__":
    main()