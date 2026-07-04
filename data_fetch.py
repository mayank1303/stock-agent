"""
Fetch layer: wraps yfinance, checks cache first, handles common failure
modes (delisted ticker, empty response, network error) instead of
crashing the whole screen when one ticker fails.
"""

import logging
import time

import yfinance as yf

from db import get_connection, is_cache_fresh, load_prices, save_prices

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("data_fetch")


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
            df = yf.Ticker(ticker).history(period="max", interval="1d")
            if df.empty:
                raise FetchError(f"No data returned for {ticker} (possibly delisted or invalid ticker)")
            return df
        except Exception as e:  # noqa: BLE001 - intentionally broad, we classify below
            last_exc = e
            logger.warning(f"Fetch attempt {attempt + 1} failed for {ticker}: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
    raise FetchError(f"Failed to fetch {ticker} after {retries + 1} attempts: {last_exc}")


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
            info = yf.Ticker(ticker).info
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