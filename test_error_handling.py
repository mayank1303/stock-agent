"""
Error resilience stress test.

Deliberately feeds bad/edge-case input to every tool and confirms each
one fails CLEANLY (a helpful error dict) rather than crashing the whole
script or silently returning a wrong-but-plausible-looking answer.

This is pure code testing - no AI/agent involved. If something here
crashes with a raw Python traceback instead of printing a clean error
dict, that's a bug to fix before trusting this tool in the agent.

Run:
    python3 test_error_handling.py
"""

from tools import (
    get_all_time_high,
    get_all_time_low,
    get_high_low,
    get_return,
    get_stock_info,
    get_stock_snapshot,
    screen_stocks,
)
from data.nifty50 import NIFTY50


def check(label, result):
    """Print pass/fail: pass = got a clean dict (with or without 'error' key),
    fail = an exception escaped (would have already crashed before reaching here)."""
    status = "OK (clean error)" if isinstance(result, dict) and "error" in result else "OK (result)"
    print(f"[{status}] {label}: {result}")


if __name__ == "__main__":
    print("=== Invalid/fake tickers ===")
    check("get_stock_info on fake ticker", get_stock_info("FAKESTOCK123.NS"))
    check("get_return on fake ticker", get_return("FAKESTOCK123.NS", "1M"))
    check("get_high_low on fake ticker", get_high_low("FAKESTOCK123.NS", "1M"))
    check("get_all_time_high on fake ticker", get_all_time_high("FAKESTOCK123.NS"))
    check("get_stock_snapshot on fake ticker", get_stock_snapshot("FAKESTOCK123.NS"))

    print("\n=== Malformed ticker (no .NS suffix, empty string, wrong type) ===")
    check("get_return with no suffix", get_return("RELIANCE", "1M"))
    check("get_return with empty string", get_return("", "1M"))

    print("\n=== Invalid period strings ===")
    check("get_return with gibberish period", get_return("RELIANCE.NS", "banana"))
    check("get_return with empty period", get_return("RELIANCE.NS", ""))
    check("get_high_low with gibberish period", get_high_low("RELIANCE.NS", "xyz"))

    print("\n=== Edge case thresholds/directions on screening ===")
    check("screen_stocks impossible threshold (500%)", screen_stocks(NIFTY50, period="1M", direction="down", threshold=500))
    check("screen_stocks threshold=0 (should match almost everything)", screen_stocks(NIFTY50[:5], period="1M", direction="down", threshold=0))

    print("\n--- these two should raise a clear ValueError (caught below), not silently return [] ---")
    try:
        screen_stocks(NIFTY50[:5], period="1M", direction="down", threshold=-10)
        print("[FAIL] negative threshold did NOT raise - this is a bug, it should be loud")
    except ValueError as e:
        print(f"[OK (clean raise)] negative threshold correctly rejected: {e}")

    try:
        screen_stocks(NIFTY50[:5], period="1M", direction="sideways", threshold=5)
        print("[FAIL] invalid direction did NOT raise - this is a bug, it should be loud")
    except ValueError as e:
        print(f"[OK (clean raise)] invalid direction correctly rejected: {e}")

    print("\n=== Mixed valid + invalid tickers in one screen ===")
    mixed = ["RELIANCE.NS", "FAKESTOCK123.NS", "TCS.NS"]
    check("screen_stocks with one bad ticker mixed in", screen_stocks(mixed, period="1M", direction="down", threshold=0))

    print("\n=== Done. Review above: every line should say OK, none should be a raw traceback. ===")