import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Configurable API URL: falls back to localhost for local dev. Set
// VITE_API_URL in frontend/.env when deploying (Phase 4) so the built
// app points at a real server instead of localhost.
const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

const STORAGE_KEY = "stock-agent-chat-v1";

function newSessionId() {
  return "web-" + Math.random().toString(36).slice(2, 10);
}

const SUGGESTIONS = [
  "How much is Reliance up this year?",
  "Which Nifty50 stocks are down >10% this month?",
  "TCS 52-week high and low",
  "Full snapshot of HDFC Bank",
];

export default function App() {
  // Restore a previous session from localStorage on first load, if any.
  const [sessionId, setSessionId] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      return saved?.sessionId || newSessionId();
    } catch {
      return newSessionId();
    }
  });
  const [messages, setMessages] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      return saved?.messages || [];
    } catch {
      return [];
    }
  });

  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [waitingForFirst, setWaitingForFirst] = useState(false); // true in the gap before the first chunk/tool_call arrives
  const [error, setError] = useState(null);
  const [copiedIndex, setCopiedIndex] = useState(null);
  const [lastQuestion, setLastQuestion] = useState(null); // for the Retry button
  const bottomRef = useRef(null);
  const abortRef = useRef(null); // lets the Stop button cancel an in-flight stream

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Persist chat history so a page refresh doesn't lose the conversation.
  // This is a real deployed app (not a Claude.ai artifact), so
  // localStorage is fine here - it never leaves the user's own browser.
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId, messages }));
    } catch {
      /* storage full or disabled - non-fatal, chat just won't persist */
    }
  }, [sessionId, messages]);

  async function send(question) {
    const q = (question ?? input).trim();
    if (!q || busy) return;

    setInput("");
    setError(null);
    setBusy(true);
    setWaitingForFirst(true);
    setLastQuestion(q);
    setMessages((m) => [...m, { role: "user", content: q }]);

    const controller = new AbortController();
    abortRef.current = controller;

    let streamCompleted = false;

    try {
      let res;
      try {
        res = await fetch(`${API}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: q, session_id: sessionId }),
          signal: controller.signal,
        });
      } catch (e) {
        if (e.name === "AbortError") throw e; // handled in outer catch, don't relabel
        throw new Error("Can't reach the agent backend. Is it running at localhost:8000?");
      }

      if (!res.ok) {
        if (res.status === 429) {
          throw new Error("You're sending questions too fast. Wait a few seconds and try again.");
        }
        throw new Error(`Backend returned an error (HTTP ${res.status}). Check the backend terminal for details.`);
      }
      if (!res.body) {
        throw new Error("Backend didn't return a readable response stream.");
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let assistantStarted = false;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const parts = buffer.split("\n\n");
        buffer = parts.pop();

        for (const part of parts) {
          const line = part.trim();
          if (!line.startsWith("data:")) continue;

          let event;
          try {
            event = JSON.parse(line.slice(5));
          } catch {
            console.warn("Skipping malformed SSE event:", line);
            continue;
          }

          setWaitingForFirst(false);

          if (event.type === "tool_call") {
            setMessages((m) => [
              ...m.map((x) => (x.role === "tool" ? { ...x, running: false } : x)),
              { role: "tool", tool: event.tool, args: event.args, running: true },
            ]);
            assistantStarted = false;
          } else if (event.type === "text") {
            if (!assistantStarted) {
              assistantStarted = true;
              setMessages((m) => [
                ...m.map((x) => (x.role === "tool" ? { ...x, running: false } : x)),
                { role: "assistant", content: event.content },
              ]);
            } else {
              setMessages((m) => {
                const copy = [...m];
                copy[copy.length - 1] = {
                  ...copy[copy.length - 1],
                  content: copy[copy.length - 1].content + event.content,
                };
                return copy;
              });
            }
          } else if (event.type === "done") {
            streamCompleted = true;
            setMessages((m) => m.map((x) => (x.role === "tool" ? { ...x, running: false } : x)));
          } else if (event.type === "error") {
            throw new Error(event.message || "The agent hit an error while answering.");
          }
        }
      }

      if (!streamCompleted) {
        setError("Connection closed before the answer finished. The response above may be incomplete.");
      }
    } catch (e) {
      if (e.name === "AbortError") {
        // User hit Stop - not a real error, just note the answer is partial
        setMessages((m) => m.map((x) => (x.role === "tool" ? { ...x, running: false } : x)));
      } else {
        setError(e.message || "Something went wrong reaching the agent.");
        setMessages((m) => m.map((x) => (x.role === "tool" ? { ...x, running: false } : x)));
      }
    } finally {
      setBusy(false);
      setWaitingForFirst(false);
      abortRef.current = null;
    }
  }

  function stopGenerating() {
    abortRef.current?.abort();
  }

  function retry() {
    if (lastQuestion) send(lastQuestion);
  }

  async function resetSession() {
    const fresh = newSessionId();
    try {
      await fetch(`${API}/chat/reset`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: "", session_id: sessionId }),
      });
    } catch {
      /* best-effort */
    }
    setSessionId(fresh);
    setMessages([]);
    setError(null);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  function fmtArgs(args) {
    return Object.entries(args || {})
      .map(([k, v]) => `${k}=${v}`)
      .join(" ");
  }

  async function copyMessage(text, index) {
    try {
      await navigator.clipboard.writeText(text);
      setCopiedIndex(index);
      setTimeout(() => setCopiedIndex((i) => (i === index ? null : i)), 1500);
    } catch {
      /* clipboard permission denied - silently do nothing */
    }
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Stock Agent</h1>
        <span className="sub">NSE · NIFTY 50 · live via yfinance</span>
        <button onClick={resetSession}>New session</button>
      </header>

      {messages.length === 0 ? (
        <div className="empty">
          <div className="board">ASK THE TAPE</div>
          <p>Real numbers only — every answer comes from a live data lookup.</p>
          <div className="suggestions">
            {SUGGESTIONS.map((s) => (
              <button key={s} onClick={() => send(s)}>
                {s}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <div className="messages">
          {messages.map((m, i) =>
            m.role === "tool" ? (
              <div key={i} className={`tool-chip${m.running ? " running" : ""}`}>
                {m.tool} <span className="args">{fmtArgs(m.args)}</span>
              </div>
            ) : m.role === "assistant" ? (
              <div key={i} className="msg assistant">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
                <button
                  className="copy-btn"
                  onClick={() => copyMessage(m.content, i)}
                  aria-label="Copy answer"
                >
                  {copiedIndex === i ? "copied" : "copy"}
                </button>
              </div>
            ) : (
              <div key={i} className={`msg ${m.role}`}>
                {m.content}
              </div>
            )
          )}

          {waitingForFirst && (
            <div className="thinking" aria-live="polite">
              <span className="dot" />
              <span className="dot" />
              <span className="dot" />
            </div>
          )}

          {error && (
            <div className="error">
              {error}
              {lastQuestion && (
                <button className="retry-btn" onClick={retry}>
                  retry
                </button>
              )}
            </div>
          )}
          <div ref={bottomRef} />
        </div>
      )}

      <div className="input-bar">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask about any NSE stock…"
          disabled={busy}
          aria-label="Ask a stock question"
        />
        {busy ? (
          <button onClick={stopGenerating} className="stop-btn">
            STOP
          </button>
        ) : (
          <button onClick={() => send()} disabled={!input.trim()}>
            SEND
          </button>
        )}
      </div>
    </div>
  );
}