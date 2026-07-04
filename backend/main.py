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
    get_stock_snapshot,
    screen_stocks,
)
from data.nifty50 import NIFTY50
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
]

TOOL_FUNCTIONS = {
    "stock_return": get_return,
    "stock_high_low": get_high_low,
    "stock_info": get_stock_info,
    "stock_snapshot": get_stock_snapshot,
    "stock_all_time_high": get_all_time_high,
    "stock_all_time_low": get_all_time_low,
    "screen_stock_universe": lambda **kw: {"matches": screen_stocks(NIFTY50, **kw)},
}


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


def run_chat_stream(question: str, session_id: str = "default"):
    """
    Generator that yields SSE-formatted chunks. Handles Claude's
    stream -> tool_use -> execute -> continue streaming loop.

    Each yielded string is one SSE "event" line - the frontend will
    read these one at a time and append text chunks as they arrive.
    """
    messages = session_store.get_session(session_id)
    messages.append({"role": "user", "content": question})

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


@app.post("/chat")
def chat(request: ChatRequest, http_request: Request):
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not check_rate_limit(client_ip):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: max {RATE_LIMIT_MAX_REQUESTS} requests per {RATE_LIMIT_WINDOW_SECONDS}s.",
        )
    return StreamingResponse(
        run_chat_stream(request.question, request.session_id),
        media_type="text/event-stream",
    )


@app.post("/chat/reset")
def reset_session(request: ChatRequest):
    """Clear a session's conversation history (the 'new chat' button)."""
    session_store.delete_session(request.session_id)
    return {"status": "cleared", "session_id": request.session_id}


@app.get("/health")
def health():
    return {"status": "ok"}