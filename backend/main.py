"""
FastAPI backend for the stock-agent web UI.

This is the piece that replaces "Claude Desktop" from Phase 1. It does
the same job Claude Desktop was doing - taking a question, calling
Claude with our tools available, executing tool calls, streaming the
answer back - except now it's OUR server, talking to OUR future React
frontend, using the Anthropic API directly (not MCP-via-Desktop-app).

Why not reuse the MCP server (server.py) here: MCP is a protocol for
connecting a LOCAL AI client (like Claude Desktop) to local tools. A
web backend calling the Claude API directly doesn't need MCP's
client-server handshake - it can call tools.py's functions directly,
same as eval_automated.py already does. MCP stays useful for Claude
Desktop specifically; this backend is a separate, simpler path to the
same underlying tools.

Run:
    uvicorn main:app --reload --port 8000

Test without a frontend yet:
    curl -N -X POST http://localhost:8000/chat \\
      -H "Content-Type: application/json" \\
      -d '{"question": "How much is Reliance up this year?"}'
"""

import json
import os

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import sys
from pathlib import Path

# tools.py, data/, db.py etc. live one directory up (project root),
# not inside backend/ - add that to the import path.
sys.path.append(str(Path(__file__).parent.parent))

load_dotenv(Path(__file__).parent.parent / ".env")

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
from data.nifty50 import NIFTY50
from rag.rag_core import search_books
from . import session_store

app = FastAPI()

# CORS: allows specific frontend origins to call this API. Localhost
# origins always allowed (local dev). For deployment, set
# FRONTEND_URL in Render's environment vars to your real Vercel URL
# (e.g. https://stock-agent.vercel.app) - added automatically below.
# Never use allow_origins=["*"] once real users are involved; it defeats
# CORS entirely.
_allowed_origins = ["http://localhost:5173", "http://localhost:3000"]
if os.environ.get("FRONTEND_URL"):
    _allowed_origins.append(os.environ["FRONTEND_URL"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# Haiku by default: cheap and plenty capable for tool routing. Override
# with AGENT_MODEL=claude-sonnet-4-6 in .env if you want stronger
# reasoning for complex multi-stock comparisons.
MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
MAX_TOOL_ROUNDS = 5  # safety cap: prevents a runaway tool-call loop

# Phase 5: swap the "brain" behind /chat without touching the frontend
# or tools.py at all. Set LLM_PROVIDER=ollama in .env to use a local
# model (zero API cost) instead of the Claude API. See eval_local_llm.py
# for the benchmark that proved this path works (9/9 tool-routing
# accuracy on our eval set, ~9s/question vs Claude's ~1-2s).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
# Separate vision-capable local model, used ONLY when an image is
# attached (smart routing). Vision models add a vision encoder that
# makes them ~30-60% slower on text than a text-only model of the same
# size, so we don't route every text query through it - only image
# ones. qwen2.5vl chosen over LLaVA for stronger structured-visual
# (chart/table/document) understanding at the 7B size.
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "qwen2.5vl:7b")
# Always imported (not gated on LLM_PROVIDER default) since the
# provider can now be chosen per-request from the UI, not just at
# startup. If Ollama isn't installed, this import still succeeds (the
# package is separate from the running service) - the actual
# connection is only attempted when an ollama request comes in, with
# its own error handling in _run_chat_stream_ollama.
import ollama

SYSTEM_PROMPT = """
You have access to stock market tools covering NSE-listed stocks. For
ANY question involving stock prices, returns, highs/lows, or screening,
you MUST call the appropriate tool first and answer using its real
data. Never answer numeric stock questions from memory or estimation,
even if asked to guess, estimate, or skip the lookup.

Formatting:
- Use a markdown TABLE whenever the answer involves more than one
  stock, or more than one period/metric for a single stock (e.g.
  screening results, multi-period returns, comparisons). Tables read
  far better than prose for this kind of data.
- Use a short prose sentence (not a table) for a single number about
  a single stock (e.g. "what's Reliance's price").
- Bold the key number that directly answers the question.
- Keep commentary brief - the data should be easy to scan, not buried
  in paragraphs.
- Use Indian numbering conventions for large rupee figures: lakhs (1,00,000)
  and crores (1,00,00,000) - e.g. "₹1,78,131 Cr" for market cap, not
  "₹178,131,000,000". This is standard for NSE stocks and how Indian
  traders read these numbers.
- Always prefix a % return/change with an explicit + or - sign (e.g.
  "+5.23%" or "-12.4%"), even for positive numbers. Non-directional
  percentages (P/E ratio, dividend yield) don't need a sign.

News:
- For "any news on X" style questions, call stock_news. Never
  reproduce full article text - you only have headline-level data.
- MANDATORY FORMAT for every news item, no exceptions: a markdown
  table with exactly these columns: Date | Headline | Publisher. Use
  the tool's published_date value in the Date column exactly as given
  (e.g. "04 Feb 2026"). Do NOT use a bulleted list for news, even for
  a single item - always the table. A news answer with a missing Date
  column is an incomplete, incorrect answer.
- If stock_news returns no results, say plainly that there's no recent
  news, rather than implying you checked less thoroughly or making
  something up.
- Do not speculate on WHY a stock moved based on news unless a
  headline directly explains it - correlation isn't causation, and
  guessing a causal story you can't verify is a form of hallucination.

Trading book library (personal, private knowledge base):
- For questions about strategy, patterns, indicators, risk, or "what
  does my trading knowledge say about X", call book_search - this
  includes SHORT FOLLOW-UP questions referencing earlier context, like
  "what about risk management", "what about X", "tell me more", "and
  entry rules?". Never answer a trading-concept question from your own
  general knowledge instead of the books, even if a follow-up seems
  conversational - your own knowledge is generic, not what the user's
  specific books actually say.
- Cite which book each idea comes from (e.g. "According to [book
  name]..."). Paraphrase in your own words - do not quote long
  passages verbatim, even though these are the user's own personal
  copies.
- When a book defines clear, checkable criteria (e.g. "a trend is
  confirmed when X and Y"), and the user is asking whether a specific
  stock/situation meets those criteria, give a DIRECT, CLEAR answer:
  state plainly whether the criteria are met, using real data from the
  stock tools to check. Do not hedge with vague non-answers when the
  books' own logic gives a checkable answer - that defeats the point
  of consulting them.
- This is the user's personal research tool, not public investment
  advice - you can be direct about what their own frameworks conclude.
  That said, be honest when frameworks conflict, when data is
  ambiguous, or when the books themselves emphasize uncertainty/risk -
  don't manufacture false confidence beyond what the source material
  and data actually support. A trading book's rules don't guarantee
  outcomes, and saying so plainly when relevant isn't hedging, it's
  being accurate to what the books themselves would say.
- If book_search returns no results, say so plainly (could mean no
  books ingested yet, or nothing relevant in the library).
- COMPOUND QUESTIONS need BOTH book_search AND a stock tool - e.g.
  "Does [stock] meet the [concept] criteria from my books" requires
  book_search for the concept AND a stock tool for the real data, in
  the same response. The phrase "from my books"/"per my books" always
  means book_search must be called, even when the question is mostly
  about a stock.
"""

# Separate, more explicit/repetitive prompt for the local model.
# FINDING (documented in README): qwen2.5:7b skipped tool-calling
# entirely for a short, casual prompt ("give info about trent?") and
# answered from its own training data - a real hallucination, not
# caught by our eval set (which used more explicit phrasings like
# "What's the current price of Reliance?"). Smaller models generally
# need more direct, example-heavy instructions than nuanced phrasing
# a larger model like Claude handles fine - this prompt is an attempt
# to close that gap, not a guaranteed fix.
OLLAMA_SYSTEM_PROMPT = """
You are a stock data assistant. You have tools to look up REAL stock
data. You must ALWAYS use a tool for ANY question about a stock,
company, or the market - no exceptions, no matter how the question is
phrased.

This includes SHORT and CASUAL questions like:
- "info about X" -> call stock_info
- "tell me about X" -> call stock_snapshot
- "X price" -> call stock_info
- "how's X doing" -> call stock_snapshot

Do NOT answer from your training data. You do NOT know current stock
prices, company financials, or market data - that information changes
daily and your training data is outdated. If you answer without
calling a tool, your answer is WRONG, even if it sounds plausible.

Every single question in this conversation is about stock market data.
Always call a tool first. Always.

For large rupee numbers, use Indian conventions: lakhs and crores
(e.g. "₹1,78,131 Cr"), not raw Western-style numbers.

Always prefix a % return/change with + or - explicitly (e.g. "+5.23%"
or "-12.4%"), even for positive numbers.

For "any news on X" questions, call stock_news. Never reproduce full
article text.

MANDATORY: format news as a markdown table with EXACTLY these columns:
Date | Headline | Publisher. Use the published_date value exactly as
given. Never use a bulleted list for news. Never skip the Date column.

If there's no recent news, say so plainly. Don't guess WHY a stock
moved unless a headline directly says so.

You have a personal trading book library (book_search tool). You must
ALWAYS call book_search for ANY question about trading concepts,
strategy, patterns, indicators, or risk - no exceptions, no matter how
short, casual, or conversational the question is. This includes
FOLLOW-UP questions that reference earlier context, like:
- "what about risk management" -> call book_search
- "what about X" (any concept) -> call book_search
- "tell me more" -> call book_search
- "and entry rules?" -> call book_search

Do NOT answer trading-concept questions from your own general
knowledge, even if you think you know the answer. Your own knowledge
is generic and NOT what the user's specific books say - only
book_search retrieves their actual books. Answering from your own
knowledge instead of the books is WRONG, even if the answer sounds
reasonable.

Cite which book each idea comes from. Paraphrase, don't quote long
passages. When a book gives clear checkable criteria and the user
asks if a stock meets them, answer DIRECTLY - check the real data and
state clearly yes/no/why. This is personal research, not public
advice, so be direct - but stay honest when data is ambiguous rather
than forcing false confidence.

COMPOUND QUESTIONS need BOTH a book tool AND a stock tool - calling
only one is WRONG. Any question shaped like "Does [STOCK] meet the
[CONCEPT] criteria from my books" or "Is [STOCK] a good [PATTERN] per
my books" or "What would my books say about [STOCK]" REQUIRES:
1. book_search for the concept/pattern (e.g. "trend-following criteria")
2. A stock tool (stock_snapshot, stock_return, etc.) for the real data
Do this even though the question sounds like it's "just" a stock
question - the phrase "from my books" or "per my books" always means
book_search must be called too, in the SAME response, not skipped.
"""

TOOLS = [
    {
        "name": "stock_return",
        "description": "Get % price change for an NSE stock over a period (1W, 1M, 3M, 6M, YTD, 1Y, or a custom window like 45D/10W/18M/2Y, or a specific date like 2024-01-01). Omit period for all standard periods at once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "NSE ticker with .NS suffix, e.g. RELIANCE.NS"},
                "period": {"type": "string"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_high_low",
        "description": "Get highest/lowest price for an NSE stock in a trailing window (1W, 1M, YTD, 1Y, 52W, ALL, or custom like 45D).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}, "period": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_info",
        "description": "Get current company info + quote snapshot: name, sector, price, market cap, P/E, etc.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_news",
        "description": "Get recent news headlines for an NSE stock over a period. Use for 'any news on X today', 'news in the last 5 days', 'major news for X this year' (period='YTD'). Returns headline + publisher + link + date only, never full article text. No recent news is a normal, valid result, not an error.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "period": {"type": "string", "description": "e.g. '2D' (default), '5D', '10D', '1W', '1M', 'YTD' (this year), '1Y', or a date like '2024-01-01'"},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_snapshot",
        "description": "Full picture: info + returns across periods + 52W high/low + comparison vs Nifty50 index. Use for broad 'tell me about X' questions.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_all_time_high",
        "description": "True all-time high across full price history.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_all_time_low",
        "description": "True all-time low across full price history.",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "screen_stock_universe",
        "description": "Get returns for Nifty50 stocks over a period. THREE modes: (1) omit direction/threshold entirely to list ALL 50 stocks' returns, sorted best-to-worst - use this for 'show me all Nifty50 stocks' performance' or 'how did Nifty50 stocks do' questions. (2) direction='down' + threshold to find losers below a % drop. (3) direction='up' + threshold to find gainers above a % rise. IMPORTANT: this is the ONLY correct way to answer questions about multiple/all Nifty50 stocks - never call stock_return individually in a loop for each stock in the index, since you do not reliably know the current, correct list of Nifty50 tickers (constituents change, and some tickers you might recall from memory are outdated or wrong).",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "description": "e.g. 1M, YTD, 6M"},
                "direction": {"type": "string", "enum": ["up", "down"], "description": "Omit entirely to list all stocks unfiltered."},
                "threshold": {"type": "number", "description": "Only used when direction is given."},
            },
            "required": [],
        },
    },
    {
        "name": "book_search",
        "description": "Search your personal trading book library for relevant passages on a strategy, pattern, indicator, or concept (e.g. 'moving average crossover entry rules', 'position sizing for volatile stocks', 'how to identify a trend reversal'). Use this whenever a question is about trading knowledge/frameworks/strategy rather than live market data. Combine with live stock tools when relevant - e.g. checking a stock's actual data AND what your books say about the pattern it's showing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in the books - a concept, pattern, or question, not a stock ticker"},
                "top_k": {"type": "integer", "description": "how many passages to retrieve, default 5"},
            },
            "required": ["query"],
        },
    },
]

def _run_book_search(query: str, top_k: int = 5) -> dict:
    """Wraps search_books with a clean, agent-friendly response shape,
    including an explicit note when no books have been ingested yet -
    a normal state before rag/ingest_books.py has been run, not an error."""
    hits = search_books(query, top_k=top_k)
    if not hits:
        return {
            "query": query,
            "count": 0,
            "results": [],
            "note": "No results - either no books have been ingested yet "
                     "(run: python3 -m rag.ingest_books) or nothing relevant was found.",
        }
    return {
        "query": query,
        "count": len(hits),
        "results": [
            {"book": h["book"], "passage": h["text"], "relevance_distance": round(h["distance"], 3)}
            for h in hits
        ],
    }


TOOL_FUNCTIONS = {
    "stock_return": get_return,
    "stock_high_low": get_high_low,
    "stock_info": get_stock_info,
    "stock_news": get_stock_news,
    "stock_snapshot": get_stock_snapshot,
    "stock_all_time_high": get_all_time_high,
    "stock_all_time_low": get_all_time_low,
    "screen_stock_universe": lambda **kw: {"matches": screen_stocks(NIFTY50, **kw)},
    "book_search": _run_book_search,
}

# Same 6 tools as TOOLS above, but in Ollama's OpenAI-style function
# schema ({"type": "function", "function": {...}}) instead of
# Anthropic's flatter format. Execution uses the same TOOL_FUNCTIONS -
# only the schema each API expects differs, not the underlying logic.
OLLAMA_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "stock_return",
            "description": "Get % price change for an NSE stock over a period (1W, 1M, 3M, 6M, YTD, 1Y, or omit for all periods).",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string", "description": "NSE ticker with .NS suffix, e.g. RELIANCE.NS"},
                    "period": {"type": "string"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stock_high_low",
            "description": "Get highest/lowest price for an NSE stock in a trailing window (1W, 1M, YTD, 1Y, 52W, ALL).",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}, "period": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stock_info",
            "description": "Get current company info + quote snapshot: name, sector, price, market cap, P/E.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stock_news",
            "description": "Get recent news for an NSE stock over a period: '2D' (default)/'5D'/'10D' for last N days, '1M'/'YTD'/'1Y' for longer windows ('YTD' = this year), or a date. Headline + publisher + link + date only, never full article text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "period": {"type": "string", "description": "e.g. '2D', '5D', '1M', 'YTD', '2024-01-01'"},
                },
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stock_snapshot",
            "description": "Full picture: info + returns across periods + 52W high/low + comparison vs Nifty50 index.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stock_all_time_high",
            "description": "True all-time high across full price history.",
            "parameters": {
                "type": "object",
                "properties": {"ticker": {"type": "string"}},
                "required": ["ticker"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screen_stock_universe",
            "description": "Get returns for Nifty50 stocks. Omit direction/threshold for all stocks unfiltered, or set direction ('up'/'down') + threshold to filter.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {"type": "string"},
                    "direction": {"type": "string", "enum": ["up", "down"]},
                    "threshold": {"type": "number"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_search",
            "description": "Search your personal trading book library for relevant passages on a strategy, pattern, indicator, or concept. Use for questions about trading knowledge/frameworks rather than live market data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "concept/pattern/question to search for, not a ticker"},
                    "top_k": {"type": "integer", "description": "how many passages, default 5"},
                },
                "required": ["query"],
            },
        },
    },
]


# Conversation history is now persisted via session_store.py (SQLite) -
# see backend/session_store.py. The old in-memory dict here lost every
# conversation on backend restart (very noticeable with --reload, which
# restarts on every code save), which is exactly the gap we're closing.

# Basic rate limiting: caps requests per client IP per minute. This is
# a first line of defense against one runaway browser tab (a bug, a
# retry loop, or misuse) burning through API credit - NOT a substitute
# for real rate limiting (e.g. Redis-backed, per-user auth) once this
# is deployed for multiple real users in Phase 4.
import time
from collections import defaultdict

RATE_LIMIT_MAX_REQUESTS = 20
RATE_LIMIT_WINDOW_SECONDS = 60
_request_log: dict[str, list] = defaultdict(list)


def check_rate_limit(client_ip: str) -> bool:
    """Returns True if the request is allowed, False if rate-limited."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS
    _request_log[client_ip] = [t for t in _request_log[client_ip] if t > window_start]
    if len(_request_log[client_ip]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    _request_log[client_ip].append(now)
    return True


class ChatRequest(BaseModel):
    question: str
    session_id: str = "default"
    provider: str | None = None  # "anthropic" or "ollama" - overrides LLM_PROVIDER env for this request
    image_base64: str | None = None  # optional attached image (data portion only, no data: prefix)
    image_media_type: str | None = None  # e.g. "image/png", "image/jpeg" - needed by Claude's API


def _run_chat_stream_anthropic(question: str, session_id: str = "default",
                                image_base64: str | None = None, image_media_type: str | None = None):
    """
    Generator that yields SSE-formatted chunks. Handles Claude's
    stream -> tool_use -> execute -> continue streaming loop.

    Each yielded string is one SSE "event" line - the frontend will
    read these one at a time and append text chunks as they arrive.

    If image_base64 is provided, the user message becomes multimodal
    (image + text) using Claude's native vision support - Claude can
    then "see" the chart/screenshot and combine it with tool calls
    (e.g. book_search for relevant framework context).
    """
    messages = session_store.get_session(session_id)

    if image_base64:
        # Multimodal message: image block first, then the text question.
        messages.append({
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image_media_type or "image/png",
                        "data": image_base64,
                    },
                },
                {"type": "text", "text": question},
            ],
        })
    else:
        messages.append({"role": "user", "content": question})

    yield f"data: {json.dumps({'type': 'provider', 'provider': 'anthropic', 'model': MODEL})}\n\n"

    # Loop because a single question can involve MULTIPLE rounds of
    # tool calls before Claude has enough info for a final answer
    # (e.g., comparing two stocks = two separate tool calls in sequence).
    # Capped at MAX_TOOL_ROUNDS so a confused model can't loop forever
    # burning API credit.
    for _round in range(MAX_TOOL_ROUNDS):
        tool_use_blocks = []
        current_text_block_open = False

        try:
            with client.messages.stream(
                model=MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            ) as stream:
                for event in stream:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        # Stream this text chunk to the browser immediately
                        yield f"data: {json.dumps({'type': 'text', 'content': event.delta.text})}\n\n"

                    elif event.type == "content_block_stop":
                        current_text_block_open = False

                final_message = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            # API failure mid-stream (rate limit, auth, network, etc.) -
            # tell the frontend explicitly instead of just dropping the
            # connection, which used to look like a silent hang/crash.
            yield f"data: {json.dumps({'type': 'error', 'message': f'The AI service hit an error: {e}'})}\n\n"
            return

        tool_use_blocks = [b for b in final_message.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # No tool call - Claude gave a final answer, we're done.
            # Persist the full history so the next question in this
            # session has context, even across a backend restart.
            messages.append({
                "role": "assistant",
                "content": session_store.to_serializable_content(final_message.content),
            })
            session_store.save_session(session_id, messages)
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        # Tell the frontend a tool is being called (nice for showing a
        # "checking live data..." indicator instead of a silent pause)
        for call in tool_use_blocks:
            yield f"data: {json.dumps({'type': 'tool_call', 'tool': call.name, 'args': call.input})}\n\n"

        # Execute every tool call, build results, continue the loop
        messages.append({
            "role": "assistant",
            "content": session_store.to_serializable_content(final_message.content),
        })

        tool_result_blocks = []
        for call in tool_use_blocks:
            func = TOOL_FUNCTIONS.get(call.name)
            try:
                output = func(**call.input) if func else {"error": f"unknown tool {call.name}"}
            except Exception as e:
                output = {"error": f"execution failed: {e}"}
            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": json.dumps(output),
            })

        messages.append({"role": "user", "content": tool_result_blocks})
        # loop continues: stream the next round (final answer or another tool call)

    # Round cap exhausted - close the stream gracefully instead of hanging
    session_store.save_session(session_id, messages)
    yield f"data: {json.dumps({'type': 'text', 'content': 'I made several data lookups but could not converge on an answer. Please try rephrasing.'})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def _run_chat_stream_ollama(question: str, session_id: str = "default", image_base64: str | None = None):
    """
    Local-model version of the agent loop, using Ollama instead of the
    Claude API - zero cost, runs entirely on your machine.

    SMART ROUTING: if an image is attached, routes to the vision model
    (OLLAMA_VISION_MODEL) instead of the text model - vision models are
    ~30-60% slower on text, so we only use one when there's actually an
    image to see. Text-only questions keep using the faster text model.

    IMAGE + TOOLS LIMITATION: when an image is present, this does a
    direct vision analysis WITHOUT tool-calling. Ollama vision models'
    support for simultaneous image input + tool-calling is unreliable
    across models, so rather than ship something flaky, the image path
    is deliberately scoped to "look at the image and answer" only. (The
    Claude path CAN combine image + tools - that's one real advantage
    of the paid provider, noted honestly for the user.)

    KNOWN SIMPLIFICATION vs the Anthropic path: tool-decision rounds are
    non-streaming (same pattern proven reliable in eval_local_llm.py).
    """
    ollama_session_id = f"ollama:{session_id}"
    messages = session_store.get_session(ollama_session_id)
    if not messages:
        messages = [{"role": "system", "content": OLLAMA_SYSTEM_PROMPT}]

    # --- IMAGE PATH: vision model, direct analysis, no tools ---
    if image_base64:
        yield f"data: {json.dumps({'type': 'provider', 'provider': 'ollama', 'model': OLLAMA_VISION_MODEL})}\n\n"
        # Ollama's API takes images as a list of base64 strings on the message.
        vision_messages = [
            {"role": "system", "content": "You are a trading assistant analyzing a chart or financial "
                                           "image the user uploaded. Describe what you see (patterns, "
                                           "levels, trends, notable features) and answer their question. "
                                           "Be honest about what is and isn't clearly visible - don't "
                                           "invent precise price numbers you can't actually read."},
            {"role": "user", "content": question, "images": [image_base64]},
        ]
        try:
            # An encoded image expands into a large block of visual tokens
            # that, with the prompt, exceeds Ollama's default 4096-token
            # context (confirmed via real testing: "4106 tokens exceeds
            # the available context size 4096"). num_ctx raises the window
            # so an image request fits. 8192 is a safe headroom for a
            # single chart screenshot; very large/multiple images might
            # need more, but this covers the normal case.
            response = ollama.chat(
                model=OLLAMA_VISION_MODEL,
                messages=vision_messages,
                options={"num_ctx": 8192},
            )
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': f'Local vision model error: {e}. Is it pulled? (ollama pull qwen2.5vl:7b)'})}\n\n"
            return
        raw_msg = response["message"]
        msg = raw_msg.model_dump() if hasattr(raw_msg, "model_dump") else dict(raw_msg)
        answer = msg.get("content", "")
        # Persist to text-model history as plain text (the image itself
        # isn't stored - keeps history light and avoids format issues).
        messages.append({"role": "user", "content": f"[uploaded an image] {question}"})
        messages.append({"role": "assistant", "content": answer})
        session_store.save_session(ollama_session_id, messages)
        yield f"data: {json.dumps({'type': 'text', 'content': answer})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"
        return

    # --- TEXT PATH: normal tool-calling agent loop ---
    messages.append({"role": "user", "content": question})

    yield f"data: {json.dumps({'type': 'provider', 'provider': 'ollama', 'model': OLLAMA_MODEL})}\n\n"

    for _round in range(MAX_TOOL_ROUNDS):
        try:
            response = ollama.chat(model=OLLAMA_MODEL, messages=messages, tools=OLLAMA_TOOLS)
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'type': 'error', 'message': f'Local model error: {e}. Is Ollama running? (brew services start ollama)'})}\n\n"
            return

        raw_msg = response["message"]
        # Ollama's client returns a typed Message object (dict-like .get()
        # access works, but it's NOT plain-dict/JSON-serializable). Convert
        # immediately so nothing downstream - persistence, logging, a
        # future feature - trips over this the way the Claude-side
        # serialization bug did earlier in the project.
        msg = raw_msg.model_dump() if hasattr(raw_msg, "model_dump") else dict(raw_msg)
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # Final answer - deliver as one chunk (see docstring: not
            # true streaming for this provider), then persist history.
            messages.append({"role": "assistant", "content": msg.get("content", "")})
            session_store.save_session(ollama_session_id, messages)
            yield f"data: {json.dumps({'type': 'text', 'content': msg.get('content', '')})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return

        messages.append(msg)
        for call in tool_calls:
            fn_name = call["function"]["name"]
            fn_args = call["function"]["arguments"]
            yield f"data: {json.dumps({'type': 'tool_call', 'tool': fn_name, 'args': fn_args})}\n\n"

            func = TOOL_FUNCTIONS.get(fn_name)
            try:
                output = func(**fn_args) if func else {"error": f"unknown tool {fn_name}"}
            except Exception as e:
                output = {"error": f"execution failed: {e}"}
            messages.append({"role": "tool", "content": json.dumps(output)})
        # loop continues: next round decides final answer or another tool call

    session_store.save_session(ollama_session_id, messages)
    yield f"data: {json.dumps({'type': 'text', 'content': 'I made several data lookups but could not converge on an answer. Please try rephrasing.'})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


def run_chat_stream(question: str, session_id: str = "default", provider: str | None = None,
                    image_base64: str | None = None, image_media_type: str | None = None):
    """Dispatches to the requested provider, falling back to the
    LLM_PROVIDER env default if the request didn't specify one."""
    active_provider = provider or LLM_PROVIDER
    if active_provider == "ollama":
        yield from _run_chat_stream_ollama(question, session_id, image_base64)
    else:
        yield from _run_chat_stream_anthropic(question, session_id, image_base64, image_media_type)


@app.post("/chat")
def chat(request: ChatRequest, http_request: Request):
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s.",
        )
    return StreamingResponse(
        run_chat_stream(request.question, request.session_id, request.provider,
                        request.image_base64, request.image_media_type),
        media_type="text/event-stream",
    )


@app.post("/chat/reset")
def reset_session(request: ChatRequest):
    """Clear a session's conversation history (the 'new chat' button).
    Clears both provider namespaces since a session_id could have history
    under either, depending on which LLM_PROVIDER was active when used."""
    session_store.delete_session(request.session_id)
    session_store.delete_session(f"ollama:{request.session_id}")
    return {"status": "cleared", "session_id": request.session_id}


@app.get("/health")
def health():
    return {"status": "ok"}