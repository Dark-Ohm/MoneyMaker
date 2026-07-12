from __future__ import annotations

import os
from pathlib import Path

import duckdb


def _data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "MoneyMaker"
    d.mkdir(parents=True, exist_ok=True)
    return d


class Database:
    def __init__(self) -> None:
        self._path = _data_dir() / "logs.db"
        self._conn = duckdb.connect(str(self._path))
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                id INTEGER,
                timestamp TIMESTAMP NOT NULL,
                market VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                price DOUBLE NOT NULL,
                volume_24h DOUBLE DEFAULT 0,
                change_pct DOUBLE DEFAULT 0,
                extra JSON
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER,
                timestamp TIMESTAMP NOT NULL,
                agent_id VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                confidence DOUBLE NOT NULL,
                reasoning TEXT DEFAULT '',
                metadata JSON
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER,
                timestamp TIMESTAMP NOT NULL,
                level VARCHAR NOT NULL DEFAULT 'info',
                source VARCHAR NOT NULL DEFAULT 'system',
                message TEXT NOT NULL
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                condition_id VARCHAR,
                slug VARCHAR,
                title TEXT,
                yes_token VARCHAR,
                no_token VARCHAR,
                resolution_rules TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS crypto_snapshots (
                timestamp TIMESTAMP,
                symbol VARCHAR,
                name VARCHAR,
                price DOUBLE,
                market_cap DOUBLE,
                volume_24h DOUBLE,
                rank INTEGER,
                percent_change_24h DOUBLE,
                raw JSON,
                PRIMARY KEY (timestamp, symbol)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_crypto_symbol_time ON crypto_snapshots(symbol, timestamp DESC)
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS news_items (
                source VARCHAR,
                title VARCHAR,
                summary VARCHAR,
                url VARCHAR PRIMARY KEY,
                published_at TIMESTAMP,
                tags JSON,
                symbols JSON,
                market_impact VARCHAR,
                raw JSON
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_news_published ON news_items(published_at DESC)
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS sec_filings (
                accession_number VARCHAR,
                filer_name VARCHAR,
                form_type VARCHAR,
                filing_date DATE,
                query VARCHAR,
                raw JSON,
                PRIMARY KEY (accession_number, query)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sec_filing_date ON sec_filings(filing_date DESC)
        """)

    def insert_tick(self, tick: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO ticks (timestamp, market, symbol, price, volume_24h, change_pct, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?::JSON)
            """,
            [
                tick["timestamp"],
                tick["market"],
                tick["symbol"],
                tick["price"],
                tick["volume_24h"],
                tick["change_pct"],
                str(tick.get("extra", "{}")),
            ],
        )

    def insert_signal(self, signal: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO signals (timestamp, agent_id, symbol, action, confidence, reasoning, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?::JSON)
            """,
            [
                signal["timestamp"],
                signal["agent_id"],
                signal["symbol"],
                signal["action"],
                signal["confidence"],
                signal.get("reasoning", ""),
                str(signal.get("metadata", "{}")),
            ],
        )

    def insert_log(self, level: str, source: str, message: str) -> None:
        from datetime import datetime

        self._conn.execute(
            "INSERT INTO logs (timestamp, level, source, message) VALUES (?, ?, ?, ?)",
            [datetime.utcnow(), level, source, message],
        )

    def query(self, sql: str, params: list | None = None) -> list[dict]:
        result = self._conn.execute(sql, params or [])
        cols = [desc[0] for desc in result.description]
        return [dict(zip(cols, row)) for row in result.fetchall()]

    def cache_market(self, meta: dict) -> None:
        # DELETE + INSERT since DuckDB doesn't support UNIQUE constraints
        self._conn.execute(
            "DELETE FROM markets WHERE condition_id = ?",
            [meta.get("condition_id", "")],
        )
        self._conn.execute(
            """
            INSERT INTO markets
                (condition_id, slug, title, yes_token, no_token, resolution_rules, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            [
                meta.get("condition_id", ""),
                meta.get("slug", ""),
                meta.get("question", ""),
                meta.get("yes_token_id", ""),
                meta.get("no_token_id", ""),
                meta.get("description", ""),
            ],
        )

    def get_cached_market(self, condition_id: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM markets WHERE condition_id = ?", [condition_id]
        )
        return rows[0] if rows else None

    def get_cached_market_by_slug(self, slug: str) -> dict | None:
        rows = self.query(
            "SELECT * FROM markets WHERE slug = ?", [slug]
        )
        return rows[0] if rows else None

    def close(self) -> None:
        self._conn.close()