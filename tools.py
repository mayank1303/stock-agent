"""
Core analysis functions over cached price data.

These are plain Python functions for now — no agent, no LLM involved.
This is intentional: get the math verifiably correct first, in a
script/REPL, before wrapping anything in an agent. Phase 1 will wrap
these as LangGraph tools without changing their logic.
"""

from datetime import date, datetime, timedelta
import re

from data_fetch import FetchError, get_history, get_history_bulk
from data_fetch import get_stock_info as _get_stock_info


def _safe_get_history(ticker: str):
    """
    Wraps get_history() to catch FetchError once, here, instead of every
    caller needing its own try/except. Returns the rows on success, or
    None on failure - callers just check `if rows is None`.

    This was a real bug found during error-handling stress testing:
    get_all_time_high/low, get_high_low, and get_return all called
    get_history() directly with no try/except, so a bad ticker crashed
    the whole process instead of returning a clean error dict.
    """
    try:
        return get_history(ticker)
    except FetchError:
        return None


def _rows_to_closes(rows):
    """rows: list of (date, open, high, low, close, volume) -> list of (date, close)"""
    return [(datetime.strptime(r[0], "%Y-%m-%d").date(), r[4]) for r in rows]


def get_stock_info(ticker: str) -> dict:
    """
    Get current company info + quote snapshot for a ticker: name,
    sector, industry, current price, day range, market cap, P/E ratio,
    dividend yield. This is live data, not from the cached history.
    """
    try:
        return _get_stock_info(ticker)
    except FetchError as e:
        return {"ticker": ticker, "error": str(e)}


NIFTY50_INDEX_TICKER = "^NSEI"  # the Nifty 50 index itself, for comparison


def get_stock_snapshot(ticker: str) -> dict:
    """
    Combined "full picture" view of a stock in one call: company info,
    returns across multiple periods, 52-week high/low, AND how those
    returns compare to the Nifty 50 index over the same periods (is
    this stock outperforming or underperforming the market, not just
    moving in isolation).
    """
    info = get_stock_info(ticker)
    if "error" in info:
        return info

    periods = ["1W", "1M", "YTD", "1Y"]
    returns = {}
    vs_nifty50 = {}

    for period in periods:
        stock_r = get_return(ticker, period)
        index_r = get_return(NIFTY50_INDEX_TICKER, period)

        if "error" not in stock_r:
            returns[period] = stock_r["pct_change"]
            if "error" not in index_r:
                # simple difference: stock return minus index return over the same window
                vs_nifty50[period] = round(stock_r["pct_change"] - index_r["pct_change"], 2)
            else:
                vs_nifty50[period] = None
        else:
            returns[period] = None
            vs_nifty50[period] = None

    high_low_52w = get_high_low(ticker, "52W")

    return {
        "ticker": ticker,
        "name": info.get("name"),
        "sector": info.get("sector"),
        "current_price": info.get("current_price"),
        "market_cap": info.get("market_cap"),
        "pe_ratio": info.get("pe_ratio"),
        "returns": returns,
        "vs_nifty50": vs_nifty50,
        "vs_nifty50_note": "Positive = outperforming the index, negative = underperforming, over the same period.",
        "52_week_high": high_low_52w.get("period_high"),
        "52_week_low": high_low_52w.get("period_low"),
        "pct_from_52w_high": high_low_52w.get("pct_from_high"),
        "pct_from_52w_low": high_low_52w.get("pct_from_low"),
    }


def get_all_time_high(ticker: str) -> dict:
    rows = _safe_get_history(ticker)
    if not rows:
        return {"ticker": ticker, "error": f"could not fetch data for {ticker} (invalid ticker or delisted)"}

    high_row = max(rows, key=lambda r: r[2])  # index 2 = high
    latest_row = rows[-1]
    ath_price = high_row[2]
    current_price = latest_row[4]
    pct_from_ath = round((current_price - ath_price) / ath_price * 100, 2)

    return {
        "ticker": ticker,
        "all_time_high": ath_price,
        "ath_date": high_row[0],
        "current_price": current_price,
        "pct_from_ath": pct_from_ath,
    }


def get_all_time_low(ticker: str) -> dict:
    rows = _safe_get_history(ticker)
    if not rows:
        return {"ticker": ticker, "error": f"could not fetch data for {ticker} (invalid ticker or delisted)"}

    low_row = min(rows, key=lambda r: r[3])  # index 3 = low
    latest_row = rows[-1]
    atl_price = low_row[3]
    current_price = latest_row[4]
    pct_from_atl = round((current_price - atl_price) / atl_price * 100, 2)

    return {
        "ticker": ticker,
        "all_time_low": atl_price,
        "atl_date": low_row[0],
        "current_price": current_price,
        "pct_from_atl": pct_from_atl,
    }


def get_high_low(ticker: str, period: str = "52W") -> dict:
    """
    Highest and lowest price hit within a given trailing window
    (e.g., "1W", "1M", "YTD", "1Y", "52W" - the standard trader
    "52-week high/low", or "ALL" for true all-time).

    This is the practical, everyday counterpart to get_all_time_high/
    get_all_time_low: those two answer "what's the record in this
    stock's entire history" (interesting but often decades old and not
    actionable). This answers "what's the high/low in a window a
    trader actually cares about right now."
    """
    rows = _safe_get_history(ticker)
    if not rows:
        return {"ticker": ticker, "error": f"could not fetch data for {ticker} (invalid ticker or delisted)"}

    if period.upper() == "ALL":
        window_rows = rows
    else:
        start_date = _period_start_date(period)
        if start_date is None:
            return {
                "ticker": ticker,
                "error": f"Could not understand period '{period}'. Use a preset "
                         f"({ALL_PERIODS + ['52W']}), a custom window like '45D'/"
                         f"'10W'/'18M'/'2Y', a specific date like '2024-01-01', or 'ALL'.",
            }
        window_rows = [
            r for r in rows
            if datetime.strptime(r[0], "%Y-%m-%d").date() >= start_date
        ]

    if not window_rows:
        return {"ticker": ticker, "error": f"not enough history for period {period}"}

    high_row = max(window_rows, key=lambda r: r[2])   # index 2 = high
    low_row = min(window_rows, key=lambda r: r[3])    # index 3 = low
    latest_row = rows[-1]
    current_price = latest_row[4]

    period_high = high_row[2]
    period_low = low_row[3]
    pct_from_high = round((current_price - period_high) / period_high * 100, 2)
    pct_from_low = round((current_price - period_low) / period_low * 100, 2)

    return {
        "ticker": ticker,
        "period": period,
        "period_high": period_high,
        "high_date": high_row[0],
        "period_low": period_low,
        "low_date": low_row[0],
        "current_price": current_price,
        "pct_from_high": pct_from_high,
        "pct_from_low": pct_from_low,
        "as_of": latest_row[0],
    }


ALL_PERIODS = ["1W", "1M", "3M", "6M", "YTD", "1Y"]

_CUSTOM_PERIOD_RE = re.compile(r"^(\d+)([DWMY])$")  # e.g. "45D", "10W", "18M", "2Y"


def _period_start_date(period: str) -> date | None:
    """
    Resolves a period string to a start date. Returns None if it can't
    be parsed - callers decide how to handle that.

    Accepts three forms:
      1. Presets: "1W", "1M", "3M", "6M", "YTD", "1Y", "52W"
      2. Custom numeric windows: "<number><unit>" where unit is
         D (days), W (weeks), M (months, approx = 30 days),
         Y (years, approx = 365 days) - e.g. "45D", "10W", "18M", "2Y"
      3. An explicit start date: "YYYY-MM-DD" - e.g. "2024-01-01"
    """
    today = date.today()
    period = period.strip().upper()

    # Form 1: presets
    if period == "1W":
        return today - timedelta(days=7)
    if period == "1M":
        return today - timedelta(days=30)
    if period == "3M":
        return today - timedelta(days=90)
    if period == "6M":
        return today - timedelta(days=182)
    if period == "YTD":
        return date(today.year, 1, 1)
    if period == "1Y":
        return today - timedelta(days=365)
    if period == "52W":
        return today - timedelta(weeks=52)

    # Form 2: custom numeric windows, e.g. "45D", "10W", "18M", "2Y"
    match = _CUSTOM_PERIOD_RE.match(period)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        if unit == "D":
            return today - timedelta(days=amount)
        if unit == "W":
            return today - timedelta(weeks=amount)
        if unit == "M":
            return today - timedelta(days=amount * 30)  # approximate, calendar months vary
        if unit == "Y":
            return today - timedelta(days=amount * 365)

    # Form 3: explicit date "YYYY-MM-DD"
    try:
        return datetime.strptime(period, "%Y-%m-%d").date()
    except ValueError:
        pass

    return None


def get_return(ticker: str, period: str | None = None) -> dict:
    """
    % change in closing price over a period.

    If period is omitted, returns % change across ALL standard periods
    (1W, 1M, 3M, 6M, YTD, 1Y) in one call - useful when the person just
    wants "how has this stock been doing" without specifying a window.

    If period is given but not recognized, returns an error listing
    valid options instead of guessing or crashing.
    """
    rows = _safe_get_history(ticker)
    if not rows:
        return {"ticker": ticker, "error": f"could not fetch data for {ticker} (invalid ticker or delisted)"}

    closes = _rows_to_closes(rows)
    latest_date, latest_close = closes[-1]

    def _return_for(p: str) -> dict | None:
        start_date = _period_start_date(p)
        if start_date is None:
            return None
        period_start_close = next((c for d, c in closes if d >= start_date), None)
        if period_start_close is None:
            return None
        pct_change = round((latest_close - period_start_close) / period_start_close * 100, 2)
        return {"start_price": period_start_close, "pct_change": pct_change}

    if period is None:
        returns = {}
        for p in ALL_PERIODS:
            r = _return_for(p)
            returns[p] = r["pct_change"] if r else None
        return {
            "ticker": ticker,
            "current_price": latest_close,
            "returns_pct": returns,
            "as_of": latest_date.isoformat(),
        }

    r = _return_for(period)
    if r is None:
        # Distinguish "couldn't parse the period at all" from "parsed fine,
        # just not enough price history to cover that window" - the fix
        # for each is different, so the error should say which one it is.
        if _period_start_date(period) is None:
            return {
                "ticker": ticker,
                "error": f"Could not understand period '{period}'. Use a preset "
                         f"({ALL_PERIODS + ['52W']}), a custom window like '45D'/'10W'/"
                         f"'18M'/'2Y', a specific date like '2024-01-01', or omit "
                         f"the period argument entirely to get all standard periods at once.",
            }
        return {"ticker": ticker, "error": f"not enough price history to cover period {period}"}

    return {
        "ticker": ticker,
        "period": period,
        "start_price": r["start_price"],
        "current_price": latest_close,
        "pct_change": r["pct_change"],
        "as_of": latest_date.isoformat(),
    }


def screen_stocks(universe: list[str], period: str = "1M", direction: str | None = None, threshold: float = 0.0) -> list[dict]:
    """
    Screen (or simply list) a universe of tickers by period return.

    direction: "down" -> only stocks with pct_change <= -threshold
               "up"   -> only stocks with pct_change >= threshold
               None   -> NO FILTER: return every stock in the universe
                         with its return, sorted best-to-worst. Use this
                         for "show me all Nifty50 stocks' performance"
                         style questions, as opposed to "which stocks
                         are down more than X%" style filtering.
    Returns results sorted by return (descending) when direction is
    None or "up"; ascending (worst first) when direction is "down".
    Tickers that fail to fetch are silently skipped (logged in data_fetch).

    Raises ValueError for an invalid direction or a negative threshold -
    deliberately loud instead of silently returning an empty list, which
    would look identical to "ran fine, nothing matched".
    """
    if direction is not None and direction not in ("down", "up"):
        raise ValueError(f"direction must be 'down', 'up', or omitted entirely, got '{direction}'")
    if threshold < 0:
        raise ValueError(f"threshold must be >= 0 (it's a magnitude, direction is separate), got {threshold}")

    history = get_history_bulk(universe)
    results = []

    for ticker in universe:
        rows = history.get(ticker)
        if not rows:
            continue
        r = get_return(ticker, period)
        if "error" in r:
            continue
        if direction is None:
            results.append(r)
        elif direction == "down" and r["pct_change"] <= -abs(threshold):
            results.append(r)
        elif direction == "up" and r["pct_change"] >= abs(threshold):
            results.append(r)

    reverse = direction != "down"  # descending unless explicitly screening for losers
    results.sort(key=lambda r: r["pct_change"], reverse=reverse)
    return results