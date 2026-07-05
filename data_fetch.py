"""
Fetch layer: wraps yfinance, checks cache first, handles common failure
modes (delisted ticker, empty response, network error) instead of
crashing the whole screen when one ticker fails.
"""

import logging
import time
from datetime import datetime, timezone

import requests
import yfinance as yf

from db import get_connection, is_cache_fresh, load_prices, save_prices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("data_fetch")

# Yahoo Finance rate-limits more aggressively on shared/datacenter IPs
# (cloud hosts like Render, Streamlit Cloud, AWS all report this - see
# yfinance GitHub issues #2125, #2411, #2422). yfinance's default
# requests look more "bot-like"; a standard browser User-Agent
# sometimes avoids triggering that defense. Not a guaranteed fix (Yahoo
# can still block by volume/IP regardless), but a real, documented,
# safe thing to try before reaching for a paid data API.
_session = requests.Session()
_session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
})


class FetchError(Exception):
    """Raised when a ticker's data can't be retrieved after retries."""


def _fetch_from_yfinance(ticker: str, retries: int = 2, backoff: float = 1.5):
    """
    Pull max-period daily history for a ticker.
    Retries on transient failures (network blips, rate limits).
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            df = yf.Ticker(ticker, session=_session).history(period="max", interval="1d")
            if df.empty:
                raise FetchError(f"No data returned for {ticker} (possibly delisted or invalid ticker)")
            return df
        except Exception as e:  # noqa: BLE001 - intentionally broad, we classify below
            last_exc = e
            logger.warning(f"Fetch attempt {attempt + 1} failed for {ticker}: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise FetchError(f"Failed to fetch {ticker} after {retries + 1} attempts: {last_exc}")


def get_stock_news(ticker: str, retries: int = 2, backoff: float = 1.5) -> list:
    """
    Fetch recent news headlines for a ticker via yfinance's built-in
    .news property. Returns a list of dicts with title/publisher/link/
    published_at only - never full article text (keeps us clean on
    copyright, and yfinance's .news doesn't give full article bodies
    anyway, just metadata + a link to the source).

    Defensive extraction: yfinance's .news response shape has changed
    across versions before (sometimes flat, sometimes nested under a
    "content" key). This tries a few known shapes rather than assuming
    one - if your installed yfinance version returns something
    different, this is the first place to look.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            raw_items = yf.Ticker(ticker, session=_session).news or []
            results = []
            for item in raw_items:
                # Some yfinance versions nest the actual fields under "content"
                content = item.get("content", item)
                title = content.get("title") or item.get("title")
                publisher = (
                    content.get("provider", {}).get("displayName")
                    if isinstance(content.get("provider"), dict)
                    else item.get("publisher")
                )
                link = (
                    content.get("canonicalUrl", {}).get("url")
                    if isinstance(content.get("canonicalUrl"), dict)
                    else item.get("link")
                )
                # publish time: unix timestamp (older shape) or ISO string (newer shape)
                # Always produce a timezone-AWARE datetime (UTC) here -
                # mixing naive and aware datetimes crashes on comparison
                # later (found via real testing: "can't compare
                # offset-naive and offset-aware datetimes" - yfinance's
                # ISO strings carry a UTC offset, but datetime.fromtimestamp()
                # defaults to naive, so the two branches disagreed).
                pub_time = item.get("providerPublishTime") or content.get("pubDate")
                if isinstance(pub_time, (int, float)):
                    published_at = datetime.fromtimestamp(pub_time, tz=timezone.utc)
                elif isinstance(pub_time, str):
                    try:
                        published_at = datetime.fromisoformat(pub_time.replace("Z", "+00:00"))
                        if published_at.tzinfo is None:
                            published_at = published_at.replace(tzinfo=timezone.utc)
                    except ValueError:
                        published_at = None
                else:
                    published_at = None

                if title:
                    results.append({
                        "title": title,
                        "publisher": publisher or "unknown",
                        "link": link,
                        "published_at": published_at,
                    })
            return results
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning(f"News fetch attempt {attempt + 1} failed for {ticker}: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)

    raise FetchError(f"Failed to fetch news for {ticker} after {retries + 1} attempts: {last_exc}")


def get_stock_info(ticker: str, retries: int = 2, backoff: float = 1.5) -> dict:
    """
    Fetch a live snapshot of company info + current quote data for a
    ticker (name, sector, current price, day range, market cap, P/E,
    dividend yield, etc.).

    Deliberately NOT cached like get_history() — this data is meant to
    reflect "right now", so caching it for a day (like price history)
    would defeat the purpose. Each call hits yfinance fresh.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            info = yf.Ticker(ticker, session=_session).info
            if not info or info.get("regularMarketPrice") is None and info.get("currentPrice") is None:
                raise FetchError(f"No quote info returned for {ticker} (possibly invalid ticker)")

            return {
                "ticker": ticker,
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "current_price": info.get("currentPrice") or info.get("regularMarketPrice"),
                "previous_close": info.get("previousClose"),
                "day_high": info.get("dayHigh"),
                "day_low": info.get("dayLow"),
                "market_cap": info.get("marketCap"),
                "pe_ratio": info.get("trailingPE"),
                "dividend_yield": info.get("dividendYield"),
                "currency": info.get("currency"),
            }
        except Exception as e:  # noqa: BLE001
            last_exc = e
            logger.warning(f"Info fetch attempt {attempt + 1} failed for {ticker}: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)

    raise FetchError(f"Failed to fetch info for {ticker} after {retries + 1} attempts: {last_exc}")


def get_history(ticker: str, force_refresh: bool = False):
    """
    Returns full price history for a ticker as a list of
    (date, open, high, low, close, volume) tuples, using the local
    cache when fresh and hitting yfinance only when needed.
    """
    conn = get_connection()
    try:
        if not force_refresh and is_cache_fresh(ticker, conn):
            logger.info(f"Cache hit: {ticker}")
            return load_prices(ticker, conn)

        logger.info(f"Cache miss/stale: {ticker} — fetching from yfinance")
        df = _fetch_from_yfinance(ticker)
        save_prices(ticker, df, conn)
        return load_prices(ticker, conn)
    finally:
        conn.close()


def get_history_bulk(tickers: list[str]) -> dict:
    """
    Fetch history for many tickers, skipping (not crashing on) any that fail.
    Returns {ticker: rows}. Failed tickers are logged and omitted —
    the caller (screening tool) should report which ones were skipped.
    """
    results = {}
    failed = []
    for ticker in tickers:
        try:
            results[ticker] = get_history(ticker)
        except FetchError as e:
            logger.error(str(e))
            failed.append(ticker)
    if failed:
        logger.warning(f"Skipped {len(failed)} tickers due to fetch errors: {failed}")
    return results