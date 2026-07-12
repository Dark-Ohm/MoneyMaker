from __future__ import annotations

import enum
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class MarketType(str, enum.Enum):
    CRYPTO = "crypto"
    STOCK = "stock"
    POLYMARKET = "polymarket"


class Tick(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    market: MarketType
    symbol: str
    price: float
    volume_24h: float = 0.0
    change_pct: float = 0.0
    extra: dict = Field(default_factory=dict)


class Signal(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    agent_id: str
    symbol: str
    action: str  # "buy" | "sell" | "hold"
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = ""
    metadata: dict = Field(default_factory=dict)


class AgentStatus(BaseModel):
    agent_id: str
    running: bool
    uptime_seconds: float = 0.0
    signals_count: int = 0


class WSMessage(BaseModel):
    type: str  # "tick" | "signal" | "log" | "status" | "error"
    data: dict


# ─── Polymarket AI Signal Schema ────────────────────────────────────────────

class AISignal(BaseModel):
    """Schema for signals emitted by AI agents. The Harness validates these
    before anything touches exchange APIs.

    CLOB V2 note: Polymarket now uses pUSD (not USDC) as collateral.
    size_pusd is the canonical field. size_usdc is accepted for backward
    compatibility but treated as pUSD internally."""

    market_type: Literal["polymarket", "crypto", "stocks"]
    asset_id: str = Field(
        ...,
        description="Condition ID or Token ID for Polymarket, ticker for crypto/stocks",
    )
    outcome: Literal["YES", "NO", "BUY", "SELL"]
    price_limit: float = Field(
        ...,
        gt=0,
        description="Max price per unit (0.01–0.99 for Polymarket, market price for others)",
    )
    size_pusd: float = Field(
        ...,
        gt=0,
        description="Position size in pUSD (CLOB V2 collateral)",
    )
    reasoning: str = ""
    agent_id: str = "default"

    @field_validator("price_limit")
    @classmethod
    def validate_polymarket_bounds(cls, v: float, info) -> float:
        if info.data.get("market_type") == "polymarket" and not (0.01 <= v <= 0.99):
            raise ValueError(
                "Polymarket outcome price must be strictly between 0.01 and 0.99"
            )
        return v

    @field_validator("outcome")
    @classmethod
    def validate_outcome_for_market(cls, v: str, info) -> str:
        market = info.data.get("market_type")
        if market == "polymarket" and v not in ("YES", "NO"):
            raise ValueError("Polymarket outcome must be YES or NO")
        if market in ("crypto", "stocks") and v not in ("BUY", "SELL"):
            raise ValueError("Crypto/stocks outcome must be BUY or SELL")
        return v

    @classmethod
    def from_legacy(cls, data: dict) -> "AISignal":
        """Accept old size_usdc field and map to size_pusd."""
        if "size_usdc" in data and "size_pusd" not in data:
            data = {**data, "size_pusd": data.pop("size_usdc")}
        return cls(**data)


class PolymarketMeta(BaseModel):
    """Metadata pulled from Gamma API for a specific market."""

    condition_id: str
    question: str
    description: str = ""
    slug: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    end_date: str = ""
    active: bool = True
    closed: bool = False
    volume_24h: float = 0.0
    liquidity: float = 0.0


class OrderbookLevel(BaseModel):
    price: float
    size: float


class OrderbookSnapshot(BaseModel):
    token_id: str
    bids: list[OrderbookLevel] = Field(default_factory=list)
    asks: list[OrderbookLevel] = Field(default_factory=list)
    spread: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class HarnessVerdict(BaseModel):
    """Result of TradingHarness processing."""

    status: str  # "APPROVED" | "REJECTED" | "CRASHED"
    signal: AISignal | None = None
    reason: str = ""
    route: str = ""  # "polymarket_clob" | "crypto_exchange" | ""
    metadata: dict = Field(default_factory=dict)
