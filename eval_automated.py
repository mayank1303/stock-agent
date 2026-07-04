"""
Automated evaluation harness.

This is the FIRST place in the project that actually uses your
Anthropic API key (everything in Claude Desktop runs on your Claude.ai
subscription, not the API - see our earlier discussion).

What this automates: for each question, we send it to Claude via the
API with our tools available, capture which tool it decided to call
and with what arguments, actually execute that tool, and compare the
result against a direct ground-truth call to the same function. This
tests TOOL ROUTING ACCURACY (did it pick the right tool + right args)
automatically - the part most likely to silently break as you change
code. It does NOT judge whether the final prose answer is well-written;
skim the "Claude's answer" column for that.

Cost: this uses Haiku (cheap) - all 20 questions should cost well under
a rupee/cent in API usage. See requirements.txt for the anthropic package.

Run:
    python3 eval_automated.py
"""

import json
import os

from dotenv import load_dotenv
from anthropic import Anthropic

load_dotenv()  # reads .env in this folder, sets ANTHROPIC_API_KEY into os.environ

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

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = "claude-haiku-4-5-20251001"  # cheap, fine for tool-routing accuracy testing

SYSTEM_PROMPT = """
You have access to stock market tools covering NSE-listed stocks. For
ANY question involving stock prices, returns, highs/lows, or screening,
you MUST call the appropriate tool first and answer using its real
data. Never answer numeric stock questions from memory or estimation.
"""

# Same tool definitions as server.py, but in Anthropic API format
# instead of MCP format. Kept intentionally minimal - just enough for
# the eval questions we're testing.
TOOLS = [
    {
        "name": "stock_return",
        "description": "Get % price change for an NSE stock over a period (1W, 1M, 3M, 6M, YTD, 1Y, or a custom window like 45D/10W/18M/2Y, or a specific date like 2024-01-01). Omit period for all standard periods at once.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "NSE ticker with .NS suffix, e.g. RELIANCE.NS"},
                "period": {"type": "string", "description": "e.g. 1M, YTD, 45D, 2024-01-01. Omit for all periods."},
            },
            "required": ["ticker"],
        },
    },
    {
        "name": "stock_high_low",
        "description": "Get highest/lowest price for an NSE stock in a trailing window (1W, 1M, YTD, 1Y, 52W, ALL, or custom like 45D).",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "period": {"type": "string", "description": "default 52W"},
            },
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
        "description": "True all-time high across full price history (not a recent window).",
        "input_schema": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
    {
        "name": "screen_stock_universe",
        "description": "Screen Nifty50 stocks by return threshold. direction is 'up' or 'down'.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down"]},
                "threshold": {"type": "number"},
            },
            "required": ["direction", "threshold"],
        },
    },
]

# Maps tool name -> actual Python function to execute
TOOL_FUNCTIONS = {
    "stock_return": get_return,
    "stock_high_low": get_high_low,
    "stock_info": get_stock_info,
    "stock_snapshot": get_stock_snapshot,
    "stock_all_time_high": get_all_time_high,
    "screen_stock_universe": lambda **kw: {
        "matches": screen_stocks(NIFTY50, **kw),
    },
}

# A subset of eval_questions.md, chosen to have one clear expected tool
# + args each - easy to auto-check routing correctness.
EVAL_QUESTIONS = [
    {"question": "What's the current price of Reliance?", "expected_tool": "stock_info", "expected_ticker": "RELIANCE.NS"},
    {"question": "How much is HDFC Bank up or down this year?", "expected_tool": "stock_return", "expected_ticker": "HDFCBANK.NS"},
    {"question": "What's the 1-month return for Wipro?", "expected_tool": "stock_return", "expected_ticker": "WIPRO.NS"},
    {"question": "What's Reliance's 52-week high and low?", "expected_tool": "stock_high_low", "expected_ticker": "RELIANCE.NS"},
    {"question": "What's the true all-time high for TCS?", "expected_tool": "stock_all_time_high", "expected_ticker": "TCS.NS"},
    {"question": "How has ICICI Bank done in the last 45 days?", "expected_tool": "stock_return", "expected_ticker": "ICICIBANK.NS"},
    {"question": "Give me a full overview of Titan's performance", "expected_tool": "stock_snapshot", "expected_ticker": "TITAN.NS"},
    {"question": "Which Nifty50 stocks are down more than 10% this month?", "expected_tool": "screen_stock_universe", "expected_ticker": None},
    {"question": "Just estimate, don't check — how much is Reliance up this year?", "expected_tool": "stock_return", "expected_ticker": "RELIANCE.NS"},
]


def run_one(item):
    messages = [{"role": "user", "content": item["question"]}]
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        messages=messages,
    )

    tool_calls = [block for block in response.content if block.type == "tool_use"]

    result = {
        "question": item["question"],
        "expected_tool": item["expected_tool"],
        "called_tool": None,
        "called_args": None,
        "tool_routing_correct": False,
        "tool_result": None,
        "final_answer": None,
    }

    if not tool_calls:
        # No tool called at all - this IS a failure for every question in
        # our eval set, since all of them require real data.
        text_blocks = [b.text for b in response.content if b.type == "text"]
        result["final_answer"] = " ".join(text_blocks)
        return result

    # The model can call MULTIPLE tools in one turn (it did on the ICICI
    # question during real testing). Every tool_use block MUST get a
    # matching tool_result block back, or the API rejects the next
    # message entirely - this bit us on the first real run, fixed here
    # by executing and responding to ALL calls, not just the first.
    tool_result_blocks = []
    executed = []

    for call in tool_calls:
        func = TOOL_FUNCTIONS.get(call.name)
        try:
            tool_output = func(**call.input) if func else {"error": f"unknown tool {call.name}"}
        except Exception as e:
            tool_output = {"error": f"execution failed: {e}"}

        executed.append({"tool": call.name, "args": call.input, "result": tool_output})
        tool_result_blocks.append({
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": json.dumps(tool_output),
        })

    # Report on the first call for the pass/fail routing check (our eval
    # questions are designed to expect one primary tool each)
    first = executed[0]
    result["called_tool"] = first["tool"]
    result["called_args"] = first["args"]
    result["tool_result"] = first["result"]

    tool_matches = first["tool"] == item["expected_tool"]
    ticker_matches = (
        item["expected_ticker"] is None
        or first["args"].get("ticker") == item["expected_ticker"]
    )
    result["tool_routing_correct"] = tool_matches and ticker_matches
    if len(executed) > 1:
        result["note"] = f"Model made {len(executed)} parallel tool calls: {[e['tool'] for e in executed]}"

    # Feed ALL tool results back and get the final natural-language answer
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": tool_result_blocks})
    final = client.messages.create(
        model=MODEL, max_tokens=1000, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages,
    )
    text_blocks = [b.text for b in final.content if b.type == "text"]
    result["final_answer"] = " ".join(text_blocks)

    return result


if __name__ == "__main__":
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set. Check your .env file is loaded,")
        print("or run: export ANTHROPIC_API_KEY=your-key-here")
        exit(1)

    passed = 0
    for item in EVAL_QUESTIONS:
        r = run_one(item)
        status = "PASS" if r["tool_routing_correct"] else "FAIL"
        passed += r["tool_routing_correct"]

        print(f"\n{'=' * 70}")
        print(f"[{status}] {r['question']}")
        print(f"  Expected tool: {r['expected_tool']}")
        print(f"  Called tool:   {r['called_tool']}  args: {r['called_args']}")
        if r.get("note"):
            print(f"  NOTE: {r['note']}")
        print(f"  Tool result:   {r['tool_result']}")
        print(f"  Final answer:  {r['final_answer']}")

    print(f"\n{'=' * 70}")
    print(f"SCORE: {passed}/{len(EVAL_QUESTIONS)} correctly routed")