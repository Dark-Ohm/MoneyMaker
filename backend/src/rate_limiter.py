"""Sliding window rate limiter backed by DuckDB.

Unlike a simple counter, this tracks individual events with timestamps,
giving accurate rate limiting even across rapid bursts. Events persist
in DuckDB so the limiter survives app restarts.

No external dependencies (no Redis, no memcached). Self-contained.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import duckdb


@dataclass
class RateLimitConfig:
    max_events: int = 30
    window_seconds: int = 60
    cleanup_interval: int = 300  # prune old events every 5 min


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter with DuckDB persistence."""

    def __init__(self, conn: duckdb.DuckDBPyConnection, config: RateLimitConfig | None = None) -> None:
        self._conn = conn
        self._config = config or RateLimitConfig()
        self._lock = threading.Lock()
        self._last_cleanup: float = 0.0
        self._init_table()

    def _init_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                id INTEGER,
                bucket VARCHAR NOT NULL,
                event_ts DOUBLE NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_rl_bucket_ts ON rate_limit_events(bucket, event_ts)"
        )

    def allow(self, bucket: str = "global") -> bool:
        """Check if an event is allowed under the rate limit.

        Returns True if allowed (and records the event).
        Returns False if rate limit exceeded.
        """
        now = time.time()
        window_start = now - self._config.window_seconds

        with self._lock:
            # Count events in window
            result = self._conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = ? AND event_ts > ?",
                [bucket, window_start],
            ).fetchone()
            count = result[0] if result else 0

            if count >= self._config.max_events:
                return False

            # Record event
            self._conn.execute(
                "INSERT INTO rate_limit_events (bucket, event_ts) VALUES (?, ?)",
                [bucket, now],
            )

            # Periodic cleanup
            if now - self._last_cleanup >= self._config.cleanup_interval:
                self._cleanup(window_start)
                self._last_cleanup = now

            return True

    def count(self, bucket: str = "global") -> int:
        """Count events in the current window."""
        now = time.time()
        window_start = now - self._config.window_seconds
        result = self._conn.execute(
            "SELECT COUNT(*) FROM rate_limit_events WHERE bucket = ? AND event_ts > ?",
            [bucket, window_start],
        ).fetchone()
        return result[0] if result else 0

    def remaining(self, bucket: str = "global") -> int:
        """How many events are allowed in the current window."""
        return max(0, self._config.max_events - self.count(bucket))

    def reset(self, bucket: str | None = None) -> None:
        """Reset rate limit events. If bucket is None, reset all."""
        with self._lock:
            if bucket:
                self._conn.execute(
                    "DELETE FROM rate_limit_events WHERE bucket = ?", [bucket]
                )
            else:
                self._conn.execute("DELETE FROM rate_limit_events")

    def _cleanup(self, before_ts: float) -> None:
        """Remove events older than the window."""
        self._conn.execute(
            "DELETE FROM rate_limit_events WHERE event_ts < ?", [before_ts]
        )
