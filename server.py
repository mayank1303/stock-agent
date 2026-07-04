"""
MCP server for the stock screening/analysis tools.

This file is a thin wrapper — it doesn't contain any new logic. Every
@mcp.tool() function below just calls a function already built and
verified in tools.py, and formats the result for an MCP client (like
Claude Desktop) to consume.

Why a wrapper instead of putting @mcp.tool() directly on tools.py's
functions: keeping tools.py free of any MCP-specific code means those
functions stay reusable elsewhere (a future web backend, a script,
tests) without dragging MCP as a dependency into places that don't
need it.

Run standalone for testing:
    python3 server.py

Then point the MCP inspector at it:
    npx @modelcontextprotocol/inspector python3 server.py
"""

import functools
import json
import time
from datetime import datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from data.nifty50 import NIFTY50
from tools import (
    get_all_time_high,
    get_all_time_low,
    get_high_low,
    get_return,
    get_stock_info,
    get_stock_snapshot,
    screen_stocks,
)

mcp = FastMCP("stock-agent")

# Maps a plain-English universe name to the actual ticker list. Add more
# entries here later (e.g., "nifty500", "us_sp500") as we expand markets.
UNIVERSES = {
    "nifty50": NIFTY50,
}

LOG_PATH = Path(__file__).parent / "tool_calls.log"


def logged_tool(func):
    """
    Decorator: records every call to a tool - what was asked, what came
    back, how long it took, and whether it succeeded - as one JSON line
    per call in tool_calls.log. This is the observability layer: it lets
    you answer "how is this agent actually being used" and "how fast/
    reliable is it" after the fact, instead of only knowing what
    happened in the moment you watched it.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.time()
        entry = {
            "timestamp": datetime.now().isoformat(),
            "tool": func.__name__,
            "args": kwargs,
        }
        try:
            result = func(*args, **kwargs)
            entry["elapsed_ms"] = round((time.time() - start) * 1000, 1)
            entry["status"] = "error" if isinstance(result, dict) and "error" in result else "success"
            return result
        except Exception as e:  # noqa: BLE001 - log then re-raise, never swallow
            entry["elapsed_ms"] = round((time.time() - start) * 1000, 1)
            entry["status"] = "exception"
            entry["error"] = str(e)
            raise
        finally:
            with open(LOG_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")

    return wrapper


@mcp.tool()
@logged_tool
def stock_snapshot(ticker: str) -> dict:
    """
    Get the FULL picture on a stock in one call: company info, current
    price, returns across multiple periods (1W, 1M, YTD, 1Y), 52-week
    high/low, AND how it's performing relative to the Nifty 50 index
    over the same periods (outperforming or underperforming the
    market). Use this when the user asks a broad "tell me about X" /
    "how is X doing" question rather than one specific metric.

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS", "TCS.NS"
    """
    return get_stock_snapshot(ticker)


@mcp.tool()
@logged_tool
def stock_info(ticker: str) -> dict:
    """
    Get current company info and quote snapshot for an NSE stock: name,
    sector, industry, current price, today's high/low, previous close,
    market cap, P/E ratio, dividend yield. Use this for "what is this
    stock" / "tell me about X" / "what's the current price of X"
    questions. This is live data, not historical.

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS", "TCS.NS"
    """
    return get_stock_info(ticker)


@mcp.tool()
@logged_tool
def stock_all_time_high(ticker: str) -> dict:
    """
    Get the true all-time high price for an NSE stock, across its
    entire available price history (not a recent window).

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS", "TCS.NS"

    Returns the all-time high price, the date it occurred, the current
    price, and the % difference between current price and that high.
    """
    return get_all_time_high(ticker)


@mcp.tool()
@logged_tool
def stock_all_time_low(ticker: str) -> dict:
    """
    Get the true all-time low price for an NSE stock, across its
    entire available price history (not a recent window).

    Note: for stocks with decades of history, this is often a very old,
    split-adjusted price and may not be a practically useful reference
    point — for a recent/actionable low, use stock_high_low instead.

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS", "TCS.NS"
    """
    return get_all_time_low(ticker)


@mcp.tool()
@logged_tool
def stock_high_low(ticker: str, period: str = "52W") -> dict:
    """
    Get the highest and lowest price an NSE stock hit within a recent
    trailing window. This is the practical, everyday version of
    high/low — use this for questions like "52-week high", "this
    month's low", "this year's high", etc.

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS"
        period: flexible - accepts presets ("1W", "1M", "3M", "6M",
            "YTD", "1Y", "52W"), custom windows like "45D"/"10W"/"18M"/
            "2Y", a specific date like "2024-01-01", or "ALL" for true
            all-time high/low.
            - "52W" (52-week high/low) is the standard trader default
    """
    return get_high_low(ticker, period)


@mcp.tool()
@logged_tool
def stock_return(ticker: str, period: str = None) -> dict:
    """
    Get the percentage price change for an NSE stock over a period.

    Args:
        ticker: NSE ticker with .NS suffix, e.g. "RELIANCE.NS"
        period: flexible - accepts:
            - presets: "1W", "1M", "3M", "6M", "YTD", "1Y"
            - custom windows: "<number><unit>", e.g. "45D" (45 days),
              "10W" (10 weeks), "18M" (18 months), "2Y" (2 years)
            - a specific start date: "2024-01-01" (since that date)
            - omitted entirely: returns ALL standard periods at once -
              use this when the person asks a general "how has this
              stock been doing" without a specific timeframe
    """
    return get_return(ticker, period)


@mcp.tool()
@logged_tool
def screen_stock_universe(
    universe: str = "nifty50",
    period: str = "1M",
    direction: str = None,
    threshold: float = 0.0,
) -> dict:
    """
    Get returns for a group of stocks. THREE modes:
    (1) Omit direction/threshold entirely to list ALL stocks in the
        universe with their returns, sorted best-to-worst - use this
        for "show me all Nifty50 stocks' performance" style questions.
    (2) direction="down" + threshold to find losers below a % drop.
    (3) direction="up" + threshold to find gainers above a % rise.

    IMPORTANT: this is the ONLY correct way to answer questions about
    multiple/all stocks in an index. Never call stock_return
    individually in a loop for each stock you think is in the index -
    you do not reliably know the current, correct constituent list
    (it changes, and tickers recalled from memory are often outdated).

    Args:
        universe: which group of stocks. Currently supported: "nifty50".
        period: one of "1W", "1M", "3M", "6M", "YTD", "1Y"
        direction: "down" for losers, "up" for gainers, omit for all
        threshold: minimum % move to qualify (only used if direction given)

    Returns all matching/listed stocks plus the count. If the universe
    name isn't recognized, returns an error instead of guessing.
    """
    ticker_list = UNIVERSES.get(universe.lower())
    if ticker_list is None:
        return {
            "error": f"Unknown universe '{universe}'. "
                     f"Supported universes: {list(UNIVERSES.keys())}"
        }

    try:
        results = screen_stocks(ticker_list, period=period, direction=direction, threshold=threshold)
    except ValueError as e:
        return {"error": str(e)}

    return {
        "universe": universe,
        "period": period,
        "direction": direction or "none (all stocks, unfiltered)",
        "threshold": threshold,
        "match_count": len(results),
        "matches": results,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")