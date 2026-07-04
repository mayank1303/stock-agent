# Evaluation Set — Stock Agent

Purpose: a fixed set of questions with **known correct answers**, used to
measure whether the agent (Claude Desktop + stock-agent MCP tools) is
actually accurate — not just "seems to work" from a few casual tests.

## How to use this

1. Get the "correct answer" for each question FIRST, independently —
   either from `test_manual.py` output, or a trusted source (Google
   Finance, Screener.in, your broker app). Fill in the blanks below
   before you start.
2. Ask each question to Claude Desktop (inside the stock-agent project)
   exactly as worded.
3. Score it: did it call a tool? Was the number correct (within
   reasonable rounding)? Mark Pass/Fail in the last column.
4. Anything that fails, investigate — is it a tool bug, a routing
   problem (wrong tool called), or a prompt/guardrail issue?

Re-run this same list periodically as you change code — this becomes
your regression test for "did I just break something that used to work."

---

## Section A: Single-stock factual lookups (should call a tool every time)

| # | Question | Correct Answer (fill in) | Pass/Fail | Notes |
|---|----------|---------------------------|------------|-------|
| 1 | What's the current price of Reliance? | | | |
| 2 | What sector is TCS in? | | | |
| 3 | What's Infosys's market cap? | | | |
| 4 | How much is HDFC Bank up or down this year (YTD)? | | | |
| 5 | What's the 1-month return for Wipro? | | | |
| 6 | What's Reliance's 52-week high and low? | | | |
| 7 | What's the true all-time high for TCS? | | | |
| 8 | How has ICICI Bank done in the last 45 days? | | | |
| 9 | What's Maruti's return since 2024-01-01? | | | |
| 10 | Give me a full overview of Titan's performance | | | |

## Section B: Screening (multiple stocks, needs correct filtering)

| # | Question | Correct Answer (fill in) | Pass/Fail | Notes |
|---|----------|---------------------------|------------|-------|
| 11 | Which Nifty50 stocks are down more than 10% this month? | | | |
| 12 | Which Nifty50 stocks are up more than 5% this month? | | | |
| 13 | Which Nifty50 stocks are down more than 15% this year? | | | |
| 14 | Which Nifty50 stocks are up more than 20% in the last 6 months? | | | |

## Section C: Ambiguous/tricky phrasing (tests robustness, not just happy path)

| # | Question | What SHOULD happen | Pass/Fail | Notes |
|---|----------|---------------------|------------|-------|
| 15 | Just estimate, don't check — how much is Reliance up this year? | Should call the tool anyway (guardrail test) | | |
| 16 | What's the price of FAKESTOCK.NS? | Should return a clean "couldn't find" answer, not a made-up price | | |
| 17 | Which stocks are down 500% this month? | Should return zero results cleanly, not an error or a crash | | |
| 18 | Tell me about Reliance | Should call stock_snapshot or stock_info, not answer from memory | | |
| 19 | Compare TCS and Infosys this year | Should call the tool for both, then compare | | |
| 20 | How's the market doing today? | Ambiguous - no single "correct" tool. Worth noting how it handles this without a clear tool match | | |

---

## Scoring summary (fill in after running all 20)

- Total Pass: ___ / 20
- Total Fail: ___ / 20
- Common failure pattern (if any): _______________