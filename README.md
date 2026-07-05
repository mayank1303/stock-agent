# Stock Agent

An agentic AI assistant for NSE (Indian stock market) analysis. Ask
natural-language questions about any Nifty50 stock — returns, highs/
lows, screening, company info — and get answers backed by real,
live-fetched data, not model guesswork.

Built as a hands-on project to learn agentic AI system design: tool-
calling, MCP, guardrails against hallucination, and a full-stack web
app around an LLM agent — end to end, including the bugs found and
fixed along the way.

## What it can do

- "What's the current price and info for Reliance?"
- "How has TCS performed over the last 45 days?"
- "Which Nifty50 stocks are down more than 10% this month?"
- "Give me a full snapshot of HDFC Bank vs the Nifty50 index"
- "Compare TCS and Infosys this year"
- Multi-turn: "What about Wipro?" (remembers context)

Every numeric answer comes from a real tool call (yfinance), never
from the model's memory — this is an explicit, tested guardrail (see
Known Limitations below for where it can still be worked around).

## Architecture

```
                    ┌─────────────────┐
                    │  Claude Desktop  │──── MCP ────┐
                    └─────────────────┘             │
                                                     ▼
┌──────────────┐   HTTP/SSE   ┌──────────────┐   ┌─────────┐   ┌──────────────┐
│ React frontend│◄────────────►│ FastAPI backend│──►│ tools.py │──►│  yfinance /  │
│  (Vercel)     │              │  (Render)     │   │(shared) │   │  SQLite cache│
└──────────────┘              └──────────────┘   └─────────┘   └──────────────┘
```

Two independent front-ends share one tool layer (`tools.py`):
- **Claude Desktop** via the MCP server (`server.py`) — for chatting
  with your own data locally, no custom UI needed
- **A custom web app** (`backend/` + `frontend/`) — a FastAPI backend
  calling the Claude API directly, with a React chat UI

Both call the exact same underlying functions, so a fix in `tools.py`
benefits both paths.

## Tech stack

- **Data**: yfinance, SQLite (local cache + session storage)
- **Agent**: Anthropic Claude API (Haiku by default), tool-calling
- **Protocols**: MCP (for Claude Desktop), plain REST+SSE (for the web app)
- **Backend**: FastAPI, Python 3.12
- **Frontend**: React, Vite, react-markdown
- **Hosting**: Render (backend), Vercel (frontend)

## Project structure

```
stock-agent/
├── data/nifty50.py          # Nifty50 ticker list
├── db.py                    # SQLite price cache layer
├── data_fetch.py            # yfinance wrapper, retries, error handling
├── tools.py                 # Core analysis functions (shared by everything)
├── server.py                # MCP server (for Claude Desktop)
├── backend/
│   ├── main.py              # FastAPI app: streaming /chat endpoint
│   └── session_store.py     # SQLite-backed conversation history
├── frontend/                # React chat UI
├── test_manual.py           # Phase 0 smoke test
├── test_error_handling.py   # Error resilience stress test
├── eval_questions.md        # Manual evaluation question set
├── eval_automated.py        # Automated tool-routing accuracy eval (uses API)
├── render.yaml               # Backend deployment config
└── DEPLOYMENT.md            # Deployment walkthrough
```

## Setup (local development)

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your ANTHROPIC_API_KEY
```

**Run the web app:**
```bash
uvicorn backend.main:app --reload --port 8000    # terminal 1
cd frontend && npm install && npm run dev         # terminal 2
```
Open http://localhost:5173

**Or use Claude Desktop instead** (no web app needed): add `server.py`
to your Claude Desktop MCP config — see project notes for the exact
config snippet.

## Testing

- `python3 test_manual.py` — verifies the data layer against real tickers
- `python3 test_error_handling.py` — confirms bad input fails cleanly, never crashes
- `python3 eval_automated.py` — scores tool-routing accuracy via the API (small cost, <$0.01)
- `eval_questions.md` — manual eval checklist for end-to-end accuracy

## Known limitations

- **Data accuracy**: yfinance prices can differ slightly from official
  NSE feeds. Fine for analysis; not wired to real trade execution.
- **Index constituents drift**: Nifty50's ticker list is a snapshot
  and needs periodic manual review (already hit this twice — Tata
  Motors' 2025 demerger to TMPV, and LTIMindtree's ticker changing to
  LTM — both were caught via real usage, not anticipated in advance).
- **Guardrail is prompt-based, not architectural**: the "always use
  real data" rule reliably holds on neutral phrasing, but can be
  overridden by an explicit in-message instruction to skip verification
  (e.g. "don't check, just estimate") in some contexts (varies by
  which client/model configuration is asking). A production system for
  higher-stakes decisions would need a harder guardrail - e.g.
  intercepting numeric questions before the model responds and forcing
  a tool call programmatically, rather than relying on the model
  choosing to comply.
- **Tool coverage gaps cause hallucination, not "AI unreliability"**:
  found in production testing - asking for unfiltered "show me all
  Nifty50 stocks' performance" caused the model to fall back on
  memorized (partially wrong) tickers, because the screening tool only
  supported *filtered* queries at the time. Root-caused and fixed by
  making the tool capable of answering the actual question, rather
  than assuming the model was just being unreliable.
- **yfinance is unreliable on cloud-hosted IPs — confirmed in production,
  not just a theoretical risk**: deployed to Render and immediately hit
  `YFRateLimitError: Too Many Requests` on every request, while the
  identical code works perfectly on a home laptop. This is a known,
  widely-reported yfinance limitation (see yfinance GitHub issues
  #2125, #2411, #2422) — Yahoo Finance is an unofficial, unlicensed
  data source (scraped, not a real API), and it rate-limits/blocks
  shared datacenter IP ranges (Render, AWS, Streamlit Cloud, etc.)
  far more aggressively than residential IPs. Tried a browser-like
  User-Agent header as a documented mitigation — did not help; Yahoo
  appears to be blocking by request volume/pattern on the IP itself,
  not just header fingerprinting.
  **Current status**: paused the public deployment rather than ship a
  visibly broken hosted demo. The project runs reliably on localhost
  (laptop IP isn't rate-limited). Revisiting hosted deployment later
  requires either accepting intermittent failures with a fallback
  message, or switching the data layer to a licensed API (Finnhub,
  Alpha Vantage) for the hosted path specifically — a real, scoped
  follow-up, not a quick fix.
- **Free-tier hosting has no persistent disk** (see DEPLOYMENT.md) -
  the price cache and chat history reset on backend restarts/redeploys.
- **Not financial advice**: this is an analysis/screening tool. It
  deliberately avoids "buy/sell" recommendations by design.

## Phase 5 findings: local LLM vs Claude API

Benchmarked `qwen2.5:7b` (via Ollama, local M4 MacBook Air) against
Claude Haiku on the same 9-question eval set (`eval_local_llm.py` vs
`eval_automated.py`):

| | Claude Haiku (API) | qwen2.5:7b (local) |
|---|---|---|
| Tool routing accuracy | 9/9 | 9/9 |
| Strict output formatting compliance | Reliable | Unreliable |
| Avg latency | ~1-2s | ~9.3s |
| Cost | Fractions of a cent | $0 |

**Takeaway**: contrary to my initial expectation (smaller open-source
models are typically less reliable at tool-calling), qwen2.5:7b
matched Claude's routing accuracy on this eval set, including
correctly handling the "just estimate, don't check" guardrail
question. The real, measured tradeoff is latency (~5-9x slower
locally), not correctness on routing — at least on this small sample.

A second, separate finding emerged later from real usage (the news
feature): tool-routing accuracy and strict output-formatting compliance
are NOT the same capability. Asked to format news as a mandatory table
with a specific column structure (including a Date column), Claude
complied correctly on the first try; qwen2.5:7b dropped the required
Date column and used a bulleted list instead, even after the system
prompt was strengthened with an explicit, mechanical template (not
just a stated preference). This held even though the same local model
routes tools perfectly — it "knows" which data to fetch, but is less
reliable about exactly how to present it. A larger, more adversarial
eval set would be needed to say this conclusively; this is a
directional signal from real usage, not a formal benchmark result.

## Roadmap

- Expand beyond Nifty50 (Nifty500, US markets)
- Classical ML forecasting layer (time-series / tree-based models)
  alongside the LLM reasoning layer
- Larger, more adversarial eval set (current one is 9-20 questions;
  a rigorous benchmark needs more, especially edge cases)