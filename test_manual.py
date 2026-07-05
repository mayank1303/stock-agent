"""
Manual smoke test for Phase 0.

Run this locally (where you have internet) to confirm data fetching,
caching, and the analysis functions all work correctly BEFORE moving
on to Phase 1 (wrapping these as agent tools). If these numbers look
wrong, fix it here first — an agent on top of broken math is just a
confident liar.

Usage:
    python test_manual.py
"""

from data.nifty50 import NIFTY50
from tools import (
    get_all_time_high,
    get_all_time_low,
    get_high_low,
    get_return,
    get_stock_info,
    get_stock_news,
    get_stock_snapshot,
    screen_stocks,
)

if __name__ == "__main__":
    print("=== Full snapshot vs Nifty50 (RELIANCE.NS) ===")
    print(get_stock_snapshot("RELIANCE.NS"))

    print("\n=== Company info snapshot (RELIANCE.NS) ===")
    print(get_stock_info("RELIANCE.NS"))

    print("\n=== NEW: Recent news (TRENT.NS, default 2D) ===")
    print(get_stock_news("TRENT.NS"))

    print("\n=== NEW: Recent news (RELIANCE.NS, last 10 days) ===")
    print(get_stock_news("RELIANCE.NS", period="10D"))

    print("\n=== NEW: Recent news (RELIANCE.NS, YTD - 'major news this year') ===")
    print(get_stock_news("RELIANCE.NS", period="YTD"))

    print("\n=== Single ticker checks (RELIANCE.NS) ===")
    print("True ATH (all history):", get_all_time_high("RELIANCE.NS"))
    print("True ATL (all history):", get_all_time_low("RELIANCE.NS"))
    print("1M return:", get_return("RELIANCE.NS", "1M"))
    print("YTD return:", get_return("RELIANCE.NS", "YTD"))

    print("\n=== NEW: get_return with NO period (should return all periods) ===")
    print(get_return("RELIANCE.NS"))

    print("\n=== NEW: get_return with truly INVALID period (should return helpful error) ===")
    print(get_return("RELIANCE.NS", "banana"))

    print("\n=== NEW: get_return with CUSTOM numeric periods ===")
    print("45D:", get_return("RELIANCE.NS", "45D"))
    print("10W:", get_return("RELIANCE.NS", "10W"))
    print("18M:", get_return("RELIANCE.NS", "18M"))

    print("\n=== NEW: get_return with SPECIFIC start date ===")
    print("Since 2024-01-01:", get_return("RELIANCE.NS", "2024-01-01"))

    print("\n=== High/Low across different windows (RELIANCE.NS) ===")
    for period in ["1W", "1M", "YTD", "1Y", "52W"]:
        print(f"  {period}:", get_high_low("RELIANCE.NS", period))

    print("\n=== Screen: Nifty50 stocks down >10% this month ===")
    losers = screen_stocks(NIFTY50, period="1M", direction="down", threshold=10.0)
    for r in losers:
        print(f"  {r['ticker']}: {r['pct_change']}% (as of {r['as_of']})")
    if not losers:
        print("  (none found — try lowering the threshold, e.g. 3-5%)")

    print("\n=== Screen: Nifty50 stocks up >5% this month ===")
    gainers = screen_stocks(NIFTY50, period="1M", direction="up", threshold=5.0)
    for r in gainers:
        print(f"  {r['ticker']}: {r['pct_change']}% (as of {r['as_of']})")
    if not gainers:
        print("  (none found — try lowering the threshold)")