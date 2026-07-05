"""
Phase 5: Local LLM benchmark.

Runs the SAME eval questions from eval_automated.py against a local
Ollama model instead of the Claude API, and reports tool-routing
accuracy + latency side by side. This is the actual test of the
tradeoff we discussed early in the project: free-and-local vs
paid-and-reliable.

Requires:
    pip install ollama
    ollama pull qwen2.5:7b   (or another tool-calling-capable model)
    Ollama running (brew services start ollama)

Run:
    python3 eval_local_llm.py
"""

import time

import ollama

from tools import (
    get_all_time_high,
    get_high_low,
    get_return,
    get_stock_info,
    get_stock_snapshot,
    screen_stocks,
)
from data.nifty50 import NIFTY50

MODEL = "qwen2.5:7b"

SYSTEM_PROMPT = """
You have access to stock market tools covering NSE-listed stocks. For
ANY question involving stock prices, returns, highs/lows, or screening,
you MUST call the appropriate tool first and answer using its real
data. Never answer numeric stock questions from memory or estimation.
"""

# Same tools as eval_automated.py, but in Ollama's OpenAI-style function
# schema (tools wrapped in {"type": "function", "function": {...}}
# rather than Anthropic's flatter {"name", "input_schema"} format.
TOOLS = [
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
]

TOOL_FUNCTIONS = {
    "stock_return": get_return,
    "stock_high_low": get_high_low,
    "stock_info": get_stock_info,
    "stock_snapshot": get_stock_snapshot,
    "stock_all_time_high": get_all_time_high,
    "screen_stock_universe": lambda **kw: {"matches": screen_stocks(NIFTY50, **kw)},
}

# Same 9 questions as eval_automated.py, so results are directly comparable.
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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": item["question"]},
    ]

    start = time.time()
    response = ollama.chat(model=MODEL, messages=messages, tools=TOOLS)
    first_call_latency = time.time() - start

    result = {
        "question": item["question"],
        "expected_tool": item["expected_tool"],
        "called_tool": None,
        "called_args": None,
        "tool_routing_correct": False,
        "tool_result": None,
        "final_answer": None,
        "latency_seconds": None,
    }

    tool_calls = response["message"].get("tool_calls") or []

    if not tool_calls:
        # No tool called - a failure for every question in this eval set
        result["final_answer"] = response["message"].get("content", "")
        result["latency_seconds"] = round(first_call_latency, 2)
        return result

    call = tool_calls[0]
    fn_name = call["function"]["name"]
    fn_args = call["function"]["arguments"]  # ollama gives this as a dict already

    result["called_tool"] = fn_name
    result["called_args"] = fn_args

    func = TOOL_FUNCTIONS.get(fn_name)
    try:
        tool_output = func(**fn_args) if func else {"error": f"unknown tool {fn_name}"}
    except Exception as e:
        tool_output = {"error": f"execution failed: {e}"}
    result["tool_result"] = tool_output

    tool_matches = fn_name == item["expected_tool"]
    ticker_matches = (
        item["expected_ticker"] is None
        or fn_args.get("ticker") == item["expected_ticker"]
    )
    result["tool_routing_correct"] = tool_matches and ticker_matches

    # Feed the tool result back for the final natural-language answer
    messages.append(response["message"])
    messages.append({
        "role": "tool",
        "content": str(tool_output),
    })

    final_start = time.time()
    final = ollama.chat(model=MODEL, messages=messages, tools=TOOLS)
    total_latency = first_call_latency + (time.time() - final_start)

    result["final_answer"] = final["message"].get("content", "")
    result["latency_seconds"] = round(total_latency, 2)

    return result


if __name__ == "__main__":
    passed = 0
    total_latency = 0.0

    for item in EVAL_QUESTIONS:
        r = run_one(item)
        status = "PASS" if r["tool_routing_correct"] else "FAIL"
        passed += r["tool_routing_correct"]
        total_latency += r["latency_seconds"] or 0

        print(f"\n{'=' * 70}")
        print(f"[{status}] {r['question']}")
        print(f"  Expected tool: {r['expected_tool']}")
        print(f"  Called tool:   {r['called_tool']}  args: {r['called_args']}")
        print(f"  Tool result:   {r['tool_result']}")
        print(f"  Final answer:  {r['final_answer']}")
        print(f"  Latency:       {r['latency_seconds']}s")

    print(f"\n{'=' * 70}")
    print(f"SCORE: {passed}/{len(EVAL_QUESTIONS)} correctly routed")
    print(f"AVG LATENCY: {round(total_latency / len(EVAL_QUESTIONS), 2)}s per question")
    print(f"COST: $0 (local inference)")
    print(f"\nCompare against eval_automated.py's Claude results: 9/9 routed correctly.")