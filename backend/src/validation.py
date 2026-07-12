from __future__ import annotations

import logging
import threading
import time

from .models import AISignal, HarnessVerdict
from .rate_limiter import SlidingWindowRateLimiter, RateLimitConfig

logger = logging.getLogger("moneymaker.harness")


class TradingHarnessConfig:
    def __init__(
        self,
        max_risk_per_trade: float = 500.0,
        max_total_exposure: float = 5_000.0,
        max_polymarket_price: float = 0.95,
        min_polymarket_price: float = 0.05,
        blocked_assets: list[str] | None = None,
        dry_run: bool = True,
        max_signals_per_minute: int = 30,
    ) -> None:
        self.max_risk_per_trade = max_risk_per_trade
        self.max_total_exposure = max_total_exposure
        self.max_polymarket_price = max_polymarket_price
        self.min_polymarket_price = min_polymarket_price
        self.blocked_assets = blocked_assets or []
        self.dry_run = dry_run
        self._current_exposure: float = 0.0
        self.max_signals_per_minute = max_signals_per_minute


class TradingHarness:
    """Deterministic validation gate between AI agents and exchange APIs.

    Every signal from every agent MUST pass through verify_and_route().
    If the AI outputs garbage — the Harness catches it here, never reaching
    any exchange endpoint.

    Rate limiting uses a sliding window backed by DuckDB (not a simple counter).
    This survives app restarts and handles burst patterns accurately.
    """

    def __init__(
        self,
        config: TradingHarnessConfig | None = None,
        executor=None,
        rate_limiter: SlidingWindowRateLimiter | None = None,
    ) -> None:
        self._config = config or TradingHarnessConfig()
        self._executor = executor
        self._rate_limiter = rate_limiter
        self._lock = threading.Lock()

    def verify_and_route(self, raw_signal: dict) -> HarnessVerdict:
        try:
            signal = AISignal.from_legacy(raw_signal)
        except Exception as e:
            return HarnessVerdict(
                status="CRASHED",
                reason=f"Schema validation failed: {e}",
            )

        # ── Risk Guard: position size (pUSD) ──
        if signal.size_pusd > self._config.max_risk_per_trade:
            return HarnessVerdict(
                status="REJECTED",
                signal=signal,
                reason=(
                    f"Size {signal.size_pusd} pUSD exceeds per-trade limit "
                    f"{self._config.max_risk_per_trade}"
                ),
            )

        # ── Risk Guard: total exposure (pUSD) ──
        if self._config._current_exposure + signal.size_pusd > self._config.max_total_exposure:
            return HarnessVerdict(
                status="REJECTED",
                signal=signal,
                reason=(
                    f"Total exposure would reach "
                    f"{self._config._current_exposure + signal.size_pusd} pUSD "
                    f"(limit {self._config.max_total_exposure})"
                ),
            )

        # ── Risk Guard: sliding window rate limit ──
        if self._rate_limiter:
            bucket = f"signals:{signal.market_type}"
            if not self._rate_limiter.allow(bucket):
                remaining = self._rate_limiter.remaining(bucket)
                return HarnessVerdict(
                    status="REJECTED",
                    signal=signal,
                    reason=(
                        f"Rate limit exceeded for {signal.market_type}: "
                        f"{self._config.max_signals_per_minute}/min, "
                        f"{remaining} remaining"
                    ),
                )
        else:
            # Fallback: in-memory counter (no DB)
            if not hasattr(self._config, "_signals_this_minute"):
                self._config._signals_this_minute = 0
            if self._config._signals_this_minute >= self._config.max_signals_per_minute:
                return HarnessVerdict(
                    status="REJECTED",
                    signal=signal,
                    reason="Rate limit exceeded (in-memory fallback)",
                )
            self._config._signals_this_minute += 1

        # ── Blocklist ──
        if signal.asset_id in self._config.blocked_assets:
            return HarnessVerdict(
                status="REJECTED",
                signal=signal,
                reason=f"Asset {signal.asset_id} is blocked",
            )

        # ── Polymarket-specific price guard ──
        if signal.market_type == "polymarket":
            if signal.price_limit < self._config.min_polymarket_price:
                return HarnessVerdict(
                    status="REJECTED",
                    signal=signal,
                    reason=(
                        f"Price {signal.price_limit} below minimum "
                        f"{self._config.min_polymarket_price}"
                    ),
                )
            if signal.price_limit > self._config.max_polymarket_price:
                return HarnessVerdict(
                    status="REJECTED",
                    signal=signal,
                    reason=(
                        f"Price {signal.price_limit} above maximum "
                        f"{self._config.max_polymarket_price}"
                    ),
                )

        # ── Route ──
        # Reserve exposure for this potential trade
        self._config._current_exposure += signal.size_pusd

        route = ""
        if signal.market_type == "polymarket":
            route = "polymarket_clob"
        elif signal.market_type == "crypto":
            route = "crypto_exchange"

        return HarnessVerdict(
            status="APPROVED",
            signal=signal,
            route=route,
        )

    async def execute_approved(self, verdict: HarnessVerdict) -> HarnessVerdict:
        """Execute an approved verdict through the CLOB executor.

        Only runs when dry_run=False and executor is configured.
        Returns updated verdict with execution result.
        """
        if verdict.status != "APPROVED":
            # Release reserved exposure for rejected/approved-but-not-executed signals
            if verdict.signal:
                self.record_success(verdict.signal.size_pusd)
            return verdict
        if self._config.dry_run:
            verdict.metadata["execution"] = "DRY_RUN"
            if verdict.signal:
                self.record_success(verdict.signal.size_pusd)  # Release for dry run
            return verdict
        if not self._executor:
            verdict.metadata["execution"] = "NO_EXECUTOR"
            if verdict.signal:
                self.record_success(verdict.signal.size_pusd)  # Release for no executor
            return verdict
        if verdict.route != "polymarket_clob":
            # Non-polymarket routes - release exposure
            if verdict.signal:
                self.record_success(verdict.signal.size_pusd)
            return verdict

        signal = verdict.signal
        if not signal:
            return verdict

        try:
            balance = await self._executor.get_balance()
            available = float(balance.get("balance", 0))
            # Insufficient balance - release exposure, trade won't happen
            if available < signal.size_pusd:
                verdict.status = "REJECTED"
                verdict.reason = (
                    f"Insufficient pUSD balance: {available} available, "
                    f"{signal.size_pusd} required"
                )
                self.record_success(signal.size_pusd)
                return verdict

            result = await self._executor.place_limit_order(
                token_id=signal.asset_id,
                side="BUY" if signal.outcome in ("YES", "BUY") else "SELL",
                price=signal.price_limit,
                size=signal.size_pusd / signal.price_limit,
            )

            verdict.metadata["execution"] = result
            if result.get("success"):
                logger.info("Order executed: %s", result.get("orderID"))
                # Release exposure on successful execution
                self.record_success(signal.size_pusd)
            else:
                verdict.status = "REJECTED"
                verdict.reason = f"CLOB order failed: {result.get('errorMsg', 'unknown')}"
                # Release exposure on failed execution too (trade didn't happen)
                self.record_success(signal.size_pusd)

        except Exception as e:
            logger.error("Execution failed: %s", e)
            verdict.status = "REJECTED"
            verdict.reason = f"Execution error: {e}"
            # Release exposure on error (trade didn't happen)
            if signal:
                self.record_success(signal.size_pusd)

        return verdict

    def reset_rate_limit(self, bucket: str | None = None) -> None:
        if self._rate_limiter:
            self._rate_limiter.reset(bucket)
        else:
            self._config._signals_this_minute = 0

    def record_success(self, size_pusd: float) -> None:
        """Record successful trade. Called after execution succeeds."""
        with self._lock:
            self._config._current_exposure = max(0, self._config._current_exposure - size_pusd)

    def get_exposure(self) -> float:
        return self._config._current_exposure
