"""
Nifty 50 constituent list (yfinance tickers, .NS suffix).

NOTE: Index constituents change periodically (NSE rebalances Nifty 50
semi-annually). This list is a snapshot — treat it as a starting point,
not a permanent source of truth. Revisit every few months, or later
automate pulling the live constituent list from NSE.

KNOWN CORRECTION (2026-07-04): Tata Motors demerged in Oct-Nov 2025 into
two separately listed companies. The original TATAMOTORS ticker was
renamed TMPV (Tata Motors Passenger Vehicles - cars/EVs/JLR). A new
entity TMCV (Tata Motors Commercial Vehicles - trucks/buses) was listed
separately. TATAMOTORS.NS no longer resolves on yfinance; using TMPV.NS
here since it's the direct successor and remains a Nifty 50 constituent.

LTIM.NS is now WRONG - LTIMindtree's current NSE symbol is LTM (verified
via TradingView, Kotak Neo, StockAnalysis.com, 2026). Corrected below.
"""

NIFTY50 = [
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS",
    "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "BPCL.NS", "BHARTIARTL.NS", "BRITANNIA.NS", "CIPLA.NS",
    "COALINDIA.NS", "DIVISLAB.NS", "DRREDDY.NS", "EICHERMOT.NS",
    "GRASIM.NS", "HCLTECH.NS", "HDFCBANK.NS", "HDFCLIFE.NS",
    "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS", "ICICIBANK.NS",
    "ITC.NS", "INDUSINDBK.NS", "INFY.NS", "JSWSTEEL.NS",
    "KOTAKBANK.NS", "LTM.NS", "LT.NS", "M&M.NS",
    "MARUTI.NS", "NTPC.NS", "NESTLEIND.NS", "ONGC.NS",
    "POWERGRID.NS", "RELIANCE.NS", "SBILIFE.NS", "SBIN.NS",
    "SUNPHARMA.NS", "TCS.NS", "TATACONSUM.NS", "TMPV.NS",
    "TATASTEEL.NS", "TECHM.NS", "TITAN.NS", "UPL.NS",
    "ULTRACEMCO.NS", "WIPRO.NS",
]