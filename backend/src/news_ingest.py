from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger("moneymaker.news")


@dataclass
class NewsItem:
    """Normalized news item from any source."""
    source: str
    title: str
    summary: str
    url: str
    published_at: datetime
    tags: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)  # mentioned tickers/coins
    market_impact: str = "unknown"  # high, medium, low, unknown
    raw: dict = field(default_factory=dict)


class CoinMarketCapClient:
    """CoinMarketCap public API (api.coinmarketcap.com) — no auth needed."""

    BASE = "https://api.coinmarketcap.com"

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(timeout=timeout)
        self._symbol_to_slug: dict[str, str] = {}

    async def close(self):
        await self._client.aclose()

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{self.BASE}{path}"
        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()

    async def top_coins(self, limit: int = 50, convert: str = "USD") -> list[dict]:
        """Fetch top N coins with prices, market cap, % changes."""
        data = await self._get(
            "/data-api/v3/cryptocurrency/listing",
            {"start": 1, "limit": limit, "convert": convert},
        )
        coins = []
        for item in data.get("data", {}).get("cryptoCurrencyList", []):
            quotes = item.get("quotes", [{}])[0]
            coins.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "symbol": item.get("symbol"),
                "slug": item.get("slug"),
                "cmc_rank": item.get("cmcRank"),
                "price": quotes.get("price"),
                "market_cap": quotes.get("marketCap"),
                "volume_24h": quotes.get("volume24h"),
                "circulating_supply": item.get("circulatingSupply"),
                "max_supply": item.get("maxSupply"),
                "percent_change_1h": quotes.get("percentChange1h"),
                "percent_change_24h": quotes.get("percentChange24h"),
                "percent_change_7d": quotes.get("percentChange7d"),
                "last_updated": quotes.get("lastUpdated"),
            })
            self._symbol_to_slug[item.get("symbol", "").upper()] = item.get("slug", "")
        return coins

    async def coin_quote(self, slug: str, convert: str = "USD") -> dict | None:
        """Get detailed quote for a coin by slug."""
        try:
            data = await self._get(
                "/data-api/v3/cryptocurrency/quote/latest",
                {"slug": slug, "convert": convert},
            )
            items = data.get("data", [])
            if not items:
                return None
            item = items[0]
            quotes = item.get("quotes", [{}])[0]
            return {
                "id": item.get("id"),
                "name": item.get("name"),
                "symbol": item.get("symbol"),
                "slug": item.get("slug"),
                "price": quotes.get("price"),
                "market_cap": quotes.get("marketCap"),
                "volume_24h": quotes.get("volume24h"),
                "percent_change_1h": quotes.get("percentChange1h"),
                "percent_change_24h": quotes.get("percentChange24h"),
                "percent_change_7d": quotes.get("percentChange7d"),
                "percent_change_30d": quotes.get("percentChange30d"),
                "last_updated": quotes.get("lastUpdated"),
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise

    async def global_metrics(self, convert: str = "USD") -> dict:
        """Get global crypto market metrics."""
        data = await self._get(
            "/data-api/v3/global-metrics/quotes/latest",
            {"convert": convert},
        )
        d = data.get("data", {})
        quotes = d.get("quotes", [{}])[0]
        return {
            "btc_dominance": d.get("btcDominance"),
            "eth_dominance": d.get("ethDominance"),
            "active_cryptocurrencies": d.get("activeCryptoCurrencies"),
            "total_cryptocurrencies": d.get("totalCryptoCurrencies"),
            "total_market_cap": quotes.get("totalMarketCap"),
            "total_volume_24h": quotes.get("totalVolume24h"),
            "defi_market_cap": d.get("defiMarketCap"),
            "stablecoin_market_cap": d.get("stablecoinMarketCap"),
            "derivatives_volume_24h": d.get("derivativesVolume24h"),
        }

    async def latest_news(self, coin_id: int | None = None, page: int = 1, size: int = 20) -> list[dict]:
        """Fetch latest crypto news, optionally filtered by coin CMC id."""
        params = {"page": page, "size": size}
        if coin_id:
            params["coins"] = coin_id
        data = await self._get("/content/v3/news", params)
        articles = []
        for item in data.get("data", []):
            meta = item.get("meta", {})
            assets = item.get("assets", [])
            coin_ids = [a.get("coinId") for a in assets if a.get("coinId")]
            articles.append({
                "title": meta.get("title"),
                "subtitle": meta.get("subtitle"),
                "slug": item.get("slug"),
                "source_name": meta.get("sourceName"),
                "source_url": meta.get("sourceUrl"),
                "cover": item.get("cover"),
                "released_at": meta.get("releasedAt") or meta.get("createdAt") or meta.get("updatedAt"),
                "coin_ids": coin_ids,
            })
        return articles


class BBCNewsClient:
    """BBC News RSS feed client."""

    FEED_URL = "https://feeds.bbci.co.uk/news/rss.xml"
    SECTION_FEEDS = {
        "world": "https://feeds.bbci.co.uk/news/world/rss.xml",
        "business": "https://feeds.bbci.co.uk/news/business/rss.xml",
        "politics": "https://feeds.bbci.co.uk/news/politics/rss.xml",
        "technology": "https://feeds.bbci.co.uk/news/technology/rss.xml",
        "health": "https://feeds.bbci.co.uk/news/health/rss.xml",
    }

    def __init__(self, timeout: float = 15.0):
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    async def fetch_front_page(self, limit: int = 30) -> list[NewsItem]:
        resp = await self._client.get(self.FEED_URL)
        resp.raise_for_status()
        return self._parse_rss(resp.text, "BBC News Front Page", limit)

    async def fetch_section(self, section: str, limit: int = 20) -> list[NewsItem]:
        url = self.SECTION_FEEDS.get(section.lower())
        if not url:
            raise ValueError(f"Unknown BBC section: {section}")
        resp = await self._client.get(url)
        resp.raise_for_status()
        return self._parse_rss(resp.text, f"BBC {section.title()}", limit)

    def _parse_rss(self, xml_text: str, source: str, limit: int) -> list[NewsItem]:
        root = ET.fromstring(xml_text)
        items = []
        for item in root.findall(".//item")[:limit]:
            title = self._clean_cdata(item.findtext("title", ""))
            description = self._clean_cdata(item.findtext("description", ""))
            link = item.findtext("link", "").strip()
            # Strip tracking params
            link = link.split("?")[0] if "?" in link else link
            # Extract article ID
            article_id = ""
            m = re.search(r"/articles/([a-z0-9]+)", link)
            if m:
                article_id = m.group(1)
            # Parse pubDate (RFC 822)
            pub_date_str = item.findtext("pubDate", "")
            published_at = self._parse_rfc822(pub_date_str)
            # Thumbnail
            thumb_url = ""
            thumb = item.find("{http://search.yahoo.com/mrss/}thumbnail")
            if thumb is not None:
                thumb_url = thumb.get("url", "")
            # Classify section from URL
            section = "news"
            if "/sport/" in link:
                section = "sport"
            elif "/sounds/" in link:
                section = "audio"
            # Skip the permanent BBC News app promo
            if re.match(r"^https?://[^/]+/news/\d+$", link):
                continue

            items.append(NewsItem(
                source=source,
                title=title,
                summary=description,
                url=link,
                published_at=published_at,
                tags=[section, "bbc"],
                raw={"article_id": article_id, "thumbnail": thumb_url}
            ))
        return items

    @staticmethod
    def _clean_cdata(text: str) -> str:
        return text.replace("<![CDATA[", "").replace("]]>", "").strip()

    @staticmethod
    def _parse_rfc822(date_str: str) -> datetime:
        # RFC 822: "Tue, 19 May 2026 11:30:55 GMT"
        try:
            dt = datetime.strptime(date_str.strip(), "%a, %d %b %Y %H:%M:%S %Z")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.now(timezone.utc)


class SECEgarClient:
    """SEC EDGAR full-text search client."""

    BASE = "https://efts.sec.gov/LATEST/search-index"

    def __init__(self, timeout: float = 30.0, user_agent: str = "MoneyMaker news@moneymaker.local"):
        self._client = httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent})

    async def close(self):
        await self._client.aclose()

    async def search(
        self,
        query: str,
        forms: list[str] | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        ciks: list[str] | None = None,
        limit: int = 100,
    ) -> dict:
        params = {"q": query}
        if forms:
            params["forms"] = ",".join(forms)
        if start_date and end_date:
            params["dateRange"] = "custom"
            params["startdt"] = start_date
            params["enddt"] = end_date
        if ciks:
            params["ciks"] = ",".join(ciks)
        params["from"] = 0

        resp = await self._client.get(self.BASE, params=params)
        if resp.status_code == 500:
            # One retry on transient 500
            await asyncio.sleep(0.5)
            resp = await self._client.get(self.BASE, params=params)
        resp.raise_for_status()
        data = resp.json()

        hits = data.get("hits", {})
        total = hits.get("total", {}).get("value", 0)
        relation = hits.get("total", {}).get("relation", "eq")

        results = []
        for hit in hits.get("hits", [])[:limit]:
            src = hit.get("_source", {})
            accession = src.get("adsh", "")
            cik = src.get("ciks", [""])[0]
            filename = hit.get("_id", "").split(":", 1)[-1] if ":" in hit.get("_id", "") else ""
            cik_int = str(int(cik)) if cik.isdigit() else cik
            acc_no_dash = accession.replace("-", "")

            # Parse filer name
            display = src.get("display_names", [""])[0]
            filer_name = display.split("  (")[0] if "  (" in display else display

            results.append({
                "accession_number": accession,
                "filer_name": filer_name,
                "filer_cik": cik,
                "all_filers": src.get("display_names", []),
                "form_type": src.get("form"),
                "filing_date": src.get("file_date"),
                "period_of_report": src.get("period_ending"),
                "sic": src.get("sics", [""])[0] if src.get("sics") else "",
                "state_of_incorporation": src.get("inc_states", [""])[0] if src.get("inc_states") else "",
                "business_state": src.get("biz_states", [""])[0] if src.get("biz_states") else "",
                "matching_file": filename,
                "document_url": f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{filename}" if filename else "",
                "filing_index_url": f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_no_dash}/{accession}-index.htm",
            })

        return {
            "success": True,
            "query": query,
            "forms": forms or [],
            "date_range": {"start": start_date, "end": end_date} if start_date and end_date else {},
            "total_results": total,
            "total_relation": relation,
            "returned": len(results),
            "results": results,
        }


class NewsIngestionService:
    """Unified news ingestion for MoneyMaker — aggregates crypto, macro, regulatory."""

    def __init__(self, db=None):
        self.db = db
        self.cmc = CoinMarketCapClient()
        self.bbc = BBCNewsClient()
        self.sec = SECEgarClient()

    async def close(self):
        await asyncio.gather(
            self.cmc.close(),
            self.bbc.close(),
            self.sec.close(),
        )

    async def fetch_crypto_snapshot(self) -> dict:
        """Get top coins, global metrics, and latest crypto news."""
        top_coins, global_metrics, crypto_news = await asyncio.gather(
            self.cmc.top_coins(limit=50),
            self.cmc.global_metrics(),
            self.cmc.latest_news(size=30),
        )
        return {
            "top_coins": top_coins,
            "global_metrics": global_metrics,
            "crypto_news": crypto_news,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }

    async def fetch_macro_news(self, sections: list[str] = None) -> list[NewsItem]:
        """Fetch BBC News front page + business/technology sections."""
        if sections is None:
            sections = ["business", "technology", "world"]
        all_items = await self.bbc.fetch_front_page(limit=30)
        for sec in sections:
            try:
                all_items.extend(await self.bbc.fetch_section(sec, limit=15))
            except Exception as e:
                logger.warning(f"BBC {sec} fetch failed: {e}")
        # Dedupe by URL
        seen = set()
        unique = []
        for item in all_items:
            if item.url not in seen:
                seen.add(item.url)
                unique.append(item)
        return unique

    async def fetch_regulatory_filings(
        self,
        queries: list[str] = None,
        forms: list[str] = None,
        days_back: int = 7,
    ) -> list[dict]:
        """Search SEC EDGAR for market-moving filings."""
        if queries is None:
            queries = [
                "material weakness",
                "going concern",
                "force majeure",
                "climate risk",
                "cybersecurity incident",
                "merger agreement",
                "acquisition",
                "bankruptcy",
                "delisting",
                "FDA approval",
                "clinical trial",
            ]
        if forms is None:
            forms = ["8-K", "10-K", "10-Q", "6-K", "20-F", "DEF 14A", "13D", "13G"]
        end_date = datetime.now().strftime("%Y-%m-%d")
        start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        all_results = []
        for q in queries:
            try:
                result = await self.sec.search(q, forms=forms, start_date=start_date, end_date=end_date, limit=20)
                if result.get("results"):
                    all_results.append({"query": q, **result})
            except Exception as e:
                logger.warning(f"SEC search '{q}' failed: {e}")
        return all_results

    async def ingest_all(self) -> dict:
        """Run full ingestion cycle — returns structured data for agent consumption."""
        crypto_snapshot, macro_news, regulatory = await asyncio.gather(
            self.fetch_crypto_snapshot(),
            self.fetch_macro_news(),
            self.fetch_regulatory_filings(),
            return_exceptions=True,
        )

        # Handle exceptions
        if isinstance(crypto_snapshot, Exception):
            logger.error(f"Crypto snapshot failed: {crypto_snapshot}")
            crypto_snapshot = {}
        if isinstance(macro_news, Exception):
            logger.error(f"Macro news failed: {macro_news}")
            macro_news = []
        if isinstance(regulatory, Exception):
            logger.error(f"Regulatory failed: {regulatory}")
            regulatory = []

        # Cache in DuckDB if available
        if self.db:
            try:
                self._cache_to_db(crypto_snapshot, macro_news, regulatory)
            except Exception as e:
                logger.warning(f"DB cache failed: {e}")

        return {
            "crypto": crypto_snapshot,
            "macro_news": [self._item_to_dict(i) for i in macro_news],
            "regulatory": regulatory,
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        }

    def _cache_to_db(self, crypto: dict, macro: list[NewsItem], regulatory: list[dict]):
        """Store latest snapshot in DuckDB for agent context."""
        # Store crypto snapshot
        if crypto.get("top_coins"):
            for coin in crypto["top_coins"]:
                self.db._conn.execute("""
                    INSERT OR REPLACE INTO crypto_snapshots
                    (timestamp, symbol, name, price, market_cap, volume_24h, rank, percent_change_24h, raw)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    crypto.get("fetched_at"),
                    coin.get("symbol"),
                    coin.get("name"),
                    coin.get("price"),
                    coin.get("market_cap"),
                    coin.get("volume_24h"),
                    coin.get("cmc_rank"),
                    coin.get("percent_change_24h"),
                    json.dumps(coin),
                ))

        # Store macro news
        for item in macro:
            self.db._conn.execute("""
                INSERT OR IGNORE INTO news_items
                (source, title, summary, url, published_at, tags, symbols, market_impact, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.source,
                item.title,
                item.summary,
                item.url,
                item.published_at.isoformat(),
                json.dumps(item.tags),
                json.dumps(item.symbols),
                item.market_impact,
                json.dumps(item.raw),
            ))

        # Store regulatory
        for batch in regulatory:
            for result in batch.get("results", []):
                self.db._conn.execute("""
                    INSERT OR IGNORE INTO sec_filings
                    (accession_number, filer_name, form_type, filing_date, query, raw)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    result.get("accession_number"),
                    result.get("filer_name"),
                    result.get("form_type"),
                    result.get("filing_date"),
                    batch.get("query"),
                    json.dumps(result),
                ))

    @staticmethod
    def _item_to_dict(item: NewsItem) -> dict:
        return {
            "source": item.source,
            "title": item.title,
            "summary": item.summary,
            "url": item.url,
            "published_at": item.published_at.isoformat(),
            "tags": item.tags,
            "symbols": item.symbols,
            "market_impact": item.market_impact,
            "raw": item.raw,
        }


# Convenience function for agent integration
async def get_market_context(db=None) -> dict:
    """One-shot fetch of all market context for agent consumption."""
    service = NewsIngestionService(db)
    try:
        return await service.ingest_all()
    finally:
        await service.close()