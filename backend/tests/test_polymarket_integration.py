"""Integration tests: Polymarket Gamma API + CLOB + Harness + DuckDB cache + Rate Limiter.

Run: cd backend && python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import tempfile

import duckdb
import pytest

from src.models import AISignal, HarnessVerdict, OrderbookSnapshot, PolymarketMeta
from src.polymarket import PolymarketClient
from src.validation import TradingHarness, TradingHarnessConfig
from src.rate_limiter import SlidingWindowRateLimiter, RateLimitConfig
from src.db import Database


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def client() -> PolymarketClient:
    return PolymarketClient(timeout=15.0)


@pytest.fixture
def harness() -> TradingHarness:
    return TradingHarness(TradingHarnessConfig(
        max_risk_per_trade=200.0,
        max_total_exposure=1_000.0,
    ))


@pytest.fixture
def db() -> Database:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "test.db")
        d = Database.__new__(Database)
        d._path = type("P", (), {"__str__": lambda _: path})()
        d._conn = duckdb.connect(path)
        d._init_tables()
        yield d
        d._conn.close()


@pytest.fixture
def rate_limiter(db: Database) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        db._conn,
        RateLimitConfig(max_events=5, window_seconds=60),
    )


# ─── Gamma API Tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_markets_returns_results(client: PolymarketClient):
    markets = await client.search_markets("president", limit=5)
    assert len(markets) > 0
    for m in markets:
        assert isinstance(m, PolymarketMeta)
        assert m.condition_id != ""
        assert m.question != ""


@pytest.mark.asyncio
async def test_market_has_both_token_ids(client: PolymarketClient):
    markets = await client.search_markets("bitcoin", limit=3)
    if not markets:
        pytest.skip("No markets found for 'bitcoin'")
    meta = markets[0]
    assert meta.yes_token_id != "", "YES token missing"
    assert meta.no_token_id != "", "NO token missing"


@pytest.mark.asyncio
async def test_get_market_by_slug(client: PolymarketClient):
    markets = await client.search_markets("trump", limit=1)
    if not markets or not markets[0].slug:
        pytest.skip("No slug available")
    meta = await client.get_market_by_slug(markets[0].slug)
    assert meta.condition_id != ""


@pytest.mark.asyncio
async def test_get_market_by_condition(client: PolymarketClient):
    markets = await client.search_markets("fed", limit=1)
    if not markets:
        pytest.skip("No markets found")
    meta = await client.get_market_by_condition(markets[0].condition_id)
    assert meta.condition_id == markets[0].condition_id


# ─── CLOB API Tests ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orderbook_returns_snapshot(client: PolymarketClient):
    markets = await client.search_markets("president", limit=3)
    if not markets:
        pytest.skip("No markets")
    meta = markets[0]
    if not meta.yes_token_id:
        pytest.skip("No YES token")
    book = await client.get_orderbook(meta.yes_token_id)
    assert isinstance(book, OrderbookSnapshot)
    assert book.token_id == meta.yes_token_id


@pytest.mark.asyncio
async def test_orderbooks_for_market(client: PolymarketClient):
    markets = await client.search_markets("president", limit=3)
    if not markets:
        pytest.skip("No markets")
    books = await client.get_orderbooks_for_market(markets[0])
    assert "YES" in books or "NO" in books


# ─── Harness Schema Validation ──────────────────────────────────────────────

def test_valid_polymarket_signal_passes(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "YES",
        "price_limit": 0.65,
        "size_pusd": 50.0,
    })
    assert verdict.status == "APPROVED"
    assert verdict.route == "polymarket_clob"
    assert verdict.signal is not None
    assert verdict.signal.outcome == "YES"


def test_legacy_usdc_field_accepted(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "YES",
        "price_limit": 0.65,
        "size_usdc": 50.0,
    })
    assert verdict.status == "APPROVED"
    assert verdict.signal.size_pusd == 50.0


def test_polymarket_price_out_of_bounds_rejects(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "YES",
        "price_limit": 1.50,
        "size_pusd": 50.0,
    })
    assert verdict.status == "CRASHED"


def test_polymarket_price_below_minimum_rejects(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "YES",
        "price_limit": 0.001,
        "size_pusd": 50.0,
    })
    assert verdict.status == "CRASHED"


def test_size_exceeds_risk_limit_rejects(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "YES",
        "price_limit": 0.50,
        "size_pusd": 500.0,
    })
    assert verdict.status == "REJECTED"
    assert "exceeds" in verdict.reason


def test_invalid_outcome_for_polymarket_rejects(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "0xabc123",
        "outcome": "BUY",
        "price_limit": 0.50,
        "size_pusd": 50.0,
    })
    assert verdict.status == "CRASHED"


def test_invalid_market_type_crashes(harness: TradingHarness):
    verdict = harness.verify_and_route({
        "market_type": "futures",
        "asset_id": "BTC",
        "outcome": "BUY",
        "price_limit": 67000,
        "size_pusd": 100.0,
    })
    assert verdict.status == "CRASHED"


def test_blocked_asset_rejects(harness: TradingHarness):
    harness._config.blocked_assets = ["SCAM_TOKEN"]
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "SCAM_TOKEN",
        "outcome": "YES",
        "price_limit": 0.50,
        "size_pusd": 10.0,
    })
    assert verdict.status == "REJECTED"
    assert "blocked" in verdict.reason


def test_exposure_accumulates():
    h = TradingHarness(TradingHarnessConfig(
        max_risk_per_trade=1_000.0,
        max_total_exposure=1_000.0,
    ))
    v1 = h.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "A",
        "outcome": "YES",
        "price_limit": 0.50,
        "size_pusd": 600.0,
    })
    assert v1.status == "APPROVED"
    verdict = h.verify_and_route({
        "market_type": "polymarket",
        "asset_id": "B",
        "outcome": "NO",
        "price_limit": 0.50,
        "size_pusd": 500.0,
    })
    assert verdict.status == "REJECTED"
    assert "exposure" in verdict.reason


# ─── Sliding Window Rate Limiter ────────────────────────────────────────────

def test_rate_limiter_allows_within_limit(rate_limiter: SlidingWindowRateLimiter):
    for _ in range(5):
        assert rate_limiter.allow("test") is True


def test_rate_limiter_blocks_over_limit(rate_limiter: SlidingWindowRateLimiter):
    for _ in range(5):
        rate_limiter.allow("test")
    assert rate_limiter.allow("test") is False


def test_rate_limiter_buckets_independent(rate_limiter: SlidingWindowRateLimiter):
    for _ in range(5):
        rate_limiter.allow("bucket_a")
    assert rate_limiter.allow("bucket_a") is False
    assert rate_limiter.allow("bucket_b") is True


def test_rate_limiter_remaining(rate_limiter: SlidingWindowRateLimiter):
    rate_limiter.allow("test")
    rate_limiter.allow("test")
    assert rate_limiter.remaining("test") == 3


def test_rate_limiter_reset(rate_limiter: SlidingWindowRateLimiter):
    for _ in range(5):
        rate_limiter.allow("test")
    assert rate_limiter.allow("test") is False
    rate_limiter.reset("test")
    assert rate_limiter.allow("test") is True


def test_harness_rejects_on_rate_limit():
    import duckdb
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = duckdb.connect(":memory:")
        rl = SlidingWindowRateLimiter(conn, RateLimitConfig(max_events=2, window_seconds=60))
        h = TradingHarness(TradingHarnessConfig(), rate_limiter=rl)

        h.verify_and_route({"market_type": "polymarket", "asset_id": "A", "outcome": "YES", "price_limit": 0.5, "size_pusd": 10})
        h.verify_and_route({"market_type": "polymarket", "asset_id": "B", "outcome": "YES", "price_limit": 0.5, "size_pusd": 10})
        v = h.verify_and_route({"market_type": "polymarket", "asset_id": "C", "outcome": "YES", "price_limit": 0.5, "size_pusd": 10})
        assert v.status == "REJECTED"
        assert "Rate limit" in v.reason


# ─── DuckDB Market Cache ────────────────────────────────────────────────────

def test_cache_market_and_retrieve(db: Database):
    db.cache_market({
        "condition_id": "0xTEST123",
        "slug": "test-market",
        "question": "Will test pass?",
        "yes_token_id": "YES_TOKEN",
        "no_token_id": "NO_TOKEN",
        "description": "Resolution: if test passes",
    })
    cached = db.get_cached_market("0xTEST123")
    assert cached is not None
    assert cached["title"] == "Will test pass?"
    assert cached["yes_token"] == "YES_TOKEN"


def test_cache_market_by_slug(db: Database):
    db.cache_market({
        "condition_id": "0xSLUG123",
        "slug": "my-slug",
        "question": "Slug market?",
        "yes_token_id": "Y",
        "no_token_id": "N",
        "description": "",
    })
    cached = db.get_cached_market_by_slug("my-slug")
    assert cached is not None
    assert cached["condition_id"] == "0xSLUG123"


def test_cache_miss_returns_none(db: Database):
    assert db.get_cached_market("NONEXISTENT") is None
    assert db.get_cached_market_by_slug("nope") is None


# ─── End-to-End: Gamma + Cache + Harness ────────────────────────────────────

@pytest.mark.asyncio
async def test_full_pipeline_with_cache(
    client: PolymarketClient, harness: TradingHarness, db: Database
):
    markets = await client.search_markets("president", limit=3)
    if not markets:
        pytest.skip("No markets")

    meta = markets[0]

    # First call: cache miss → fetch from Gamma → store in DuckDB
    db.cache_market(meta.model_dump(mode="json"))
    cached = db.get_cached_market(meta.condition_id)
    assert cached is not None

    # Second call: cache hit
    cached2 = db.get_cached_market(meta.condition_id)
    assert cached2["condition_id"] == meta.condition_id

    # Validate signal
    verdict = harness.verify_and_route({
        "market_type": "polymarket",
        "asset_id": meta.condition_id,
        "outcome": "YES",
        "price_limit": 0.55,
        "size_pusd": 25.0,
    })
    assert verdict.status == "APPROVED"
    assert verdict.signal.asset_id == meta.condition_id
