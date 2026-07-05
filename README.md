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

**Live market data (Nifty50):**
- "What's the current price and info for Reliance?"
- "How has TCS performed over the last 45 days?"
- "Which Nifty50 stocks are down more than 10% this month?"
- "Give me a full snapshot of HDFC Bank vs the Nifty50 index"
- "Compare TCS and Infosys this year"
- Multi-turn: "What about Wipro?" (remembers context)

**Recent news:**
- "Any news on Trent today or yesterday?"
- "Major news for Reliance this year?" (flexible periods: 2D, 5D, 1M, YTD, etc.)

**Personal trading-book library (RAG):**
- "What does my book say about position sizing?"
- "Does Reliance currently meet the trend-following criteria from my books?"
  (combines live data + retrieved book frameworks, cites the book by name)

**Chart image analysis:**
- Upload a chart screenshot and ask what pattern it shows, cross-referenced
  against your book library. Uses Claude's native vision, or a local
  vision model (qwen2.5-VL) when the local provider is selected.

**Provider toggle:** switch between Claude API (fast, reliable) and a
local Ollama model (free, private, slower) per-message, no restart.

Every numeric answer comes from a real tool call (yfinance), never
from the model's memory — this is an explicit, tested guardrail (see
Known Limitations for where it can still be worked around).

## Architecture

```
      ┌─────────────────┐
      │  Claude Desktop  │──── MCP ────┐
      └─────────────────┘             │
                                       ▼
┌──────────────┐  HTTP/SSE  ┌──────────────┐     ┌─────────────────────────┐
│React frontend│◄──────────►│FastAPI backend│────►│ tools.py (shared)        │
│(upload, chat,│            │(streaming,    │     │  • yfinance + SQLite cache│
│ provider tgl)│            │ 2 providers)  │     │  • news                   │
└──────────────┘            └───────┬──────┘     │  • book_search (RAG)      │
                                     │            └─────────────────────────┘
                         ┌───────────┴──────────┐
                         ▼                      ▼
                 ┌──────────────┐      ┌──────────────────┐
                 │ Claude API   │      │ Local Ollama     │
                 │ (+ vision)   │      │ (text + vision)  │
                 └──────────────┘      └──────────────────┘

RAG: books/ (PDF/EPUB) ──ingest──► ChromaDB vectors ──► book_search
```

Multiple front-ends and both LLM providers share one tool layer
(`tools.py`), so a fix there benefits every path:
- **Claude Desktop** via the MCP server (`server.py`) — chat with your
  data locally, no custom UI
- **Custom web app** (`backend/` + `frontend/`) — FastAPI backend that
  can route each request to either the Claude API or a local Ollama
  model, with streaming, image upload, and a book-library RAG tool

## Tech stack

- **Data**: yfinance, SQLite (local price cache + session storage)
- **Agent**: Anthropic Claude API (Haiku by default), tool-calling; or
  local Ollama (qwen2.5:7b text, qwen2.5-VL for images) as a free
  alternative
- **RAG**: sentence-transformers (local embeddings), ChromaDB (local
  vector store), pypdf + ebooklib + Tesseract OCR for ingestion
- **Protocols**: MCP (for Claude Desktop), plain REST+SSE (for the web app)
- **Backend**: FastAPI, Python 3.12
- **Frontend**: React, Vite, react-markdown
- **Hosting**: Render (backend), Vercel (frontend) — currently paused
  (see Known Limitations)

## Project structure

```
stock-agent/
├── data/nifty50.py          # Nifty50 ticker list
├── db.py                    # SQLite price cache layer
├── data_fetch.py            # yfinance wrapper (prices, info, news), retries, error handling
├── tools.py                 # Core analysis functions (shared by everything)
├── server.py                # MCP server (for Claude Desktop)
├── backend/
│   ├── main.py              # FastAPI app: streaming /chat, both providers, image handling
│   └── session_store.py     # SQLite-backed conversation history
├── frontend/                # React chat UI (streaming, provider toggle, image upload, watchlist, etc.)
├── rag/
│   ├── rag_core.py          # Embedding, chunking, Chroma store, OCR, offline-mode handling
│   └── ingest_books.py      # Ingest PDF/EPUB books into the vector store
├── books/                   # Personal book library (PDFs/EPUBs, gitignored)
├── test_manual.py           # Data-layer smoke test (prices, info, news)
├── test_error_handling.py   # Error resilience stress test
├── eval_questions.md        # Manual evaluation question set
├── eval_automated.py        # Automated tool-routing accuracy eval (Claude API)
├── eval_local_llm.py        # Same eval against a local Ollama model
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

### Optional: local LLM (free, no API cost)

```bash
brew install ollama && brew services start ollama
ollama pull qwen2.5:7b       # text model
ollama pull qwen2.5vl:7b     # vision model (for chart image analysis)
```
Then toggle "Local (free)" in the web app header. See Phase 5 findings
for the measured accuracy/speed/cost tradeoffs vs Claude.

### Optional: personal trading-book library (RAG)

```bash
brew install tesseract poppler   # OCR engine + PDF rendering (for scanned books)
# drop PDF/EPUB books into books/, then:
python3 -m rag.ingest_books
```
Ingestion is one-time per book (re-runs skip already-ingested files),
runs fully offline after the embedding model's first download, and
handles scanned pages, embedded diagrams, and corrupted PDFs
gracefully (see rag/rag_core.py for the defensive handling).

## Testing

- `python3 test_manual.py` — verifies the data layer (prices, info, news) against real tickers
- `python3 test_error_handling.py` — confirms bad input fails cleanly, never crashes
- `python3 eval_automated.py` — scores tool-routing accuracy via the Claude API (small cost, <$0.01)
- `python3 eval_local_llm.py` — same eval against a local Ollama model (free), for comparison
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
- **RAG retrieval quality depends on the books**: the library is
  heavily weighted toward trading *psychology* (Mark Douglas, etc.),
  which teach mindset frameworks rather than concrete numeric rules.
  So "does stock X meet criteria Y" questions often correctly return
  an honest "your books don't define a checkable rule for this" rather
  than a fabricated checklist - accurate, but only as useful as the
  source material is specific. More systematic/technical books would
  give more directly-checkable answers.
- **Conversation history contamination**: once a hallucinated or wrong
  answer enters a session's history, follow-up questions can build on
  it rather than self-correct, even with tool-use guardrails. The fix
  is starting a new session, not more prompt engineering — a real,
  documented behavior worth knowing when testing or when a bad answer
  appears in real use.
- **OCR on messy PDFs is best-effort**: downloaded book PDFs vary
  wildly in quality. The ingestion pipeline handles scanned pages,
  embedded-diagram text, corrupted image references, content-stream
  parse crashes, and hangs (via timeout + circuit breaker) - all found
  and fixed through real ingestion of a messy 20+ book library - but
  severely degraded scans may still yield imperfect OCR text.
- **Local vision model is weaker on precise numbers**: qwen2.5-VL can
  describe chart patterns/structure locally and free, but (consistent
  with the text model's numerical gap) is less reliable at reading
  exact price levels off a chart than Claude's vision. Use Claude for
  anything where exact figures matter.
- **Not financial advice**: this is an analysis/screening tool. It
  synthesizes what the user's own books' frameworks say for personal
  research, but leaves the actual capital-at-risk decision to the user
  and stays honest about market uncertainty.

## Local LLM vs Claude API: measured findings

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

A third finding, from testing the RAG + live-data combination: even
when the local model correctly calls both required tools, it can still
**misread or fabricate individual numbers** from the returned data -
e.g. stating a 1-week return as "+23%" when the tool actually returned
"+0.23%" (a 100x decimal error), inverting the sign convention on a
relative-performance field (reading a negative "underperforming" value
as "outperforming"), and inventing a comparison figure that wasn't in
the tool's output at all. Claude, given the identical tool data in the
same test, reported every number correctly. This is more concerning
than simply skipping a tool call - the local model's response *looked*
well-grounded (real tool chips, real citations) while still containing
fabricated/misread figures, which is harder for a user to catch than
an obvious "no tool used" skip. This looks like a genuine multi-field
numerical reasoning gap rather than something fixable with more prompt
engineering.

## Roadmap

- Expand beyond Nifty50 (Nifty500, US markets)
- Classical ML forecasting layer (time-series / tree-based models)
  alongside the LLM reasoning layer
- Larger, more adversarial eval set (current one is 9-20 questions;
  a rigorous benchmark needs more, especially edge cases)