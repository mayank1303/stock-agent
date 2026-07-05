import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { NIFTY50_META } from "./nifty50Meta";
import { GLOSSARY } from "./glossary";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

const STORAGE_KEY = "stock-agent-chat-v1";
const WATCHLIST_KEY = "stock-agent-watchlist-v1";
const ONBOARDED_KEY = "stock-agent-onboarded-v1";

function newSessionId() {
  return "web-" + Math.random().toString(36).slice(2, 10);
}

const SUGGESTIONS = [
  "How much is Reliance up this year?",
  "Which Nifty50 stocks are down >10% this month?",
  "TCS 52-week high and low",
  "Full snapshot of HDFC Bank",
];

// Splits text on explicit-sign percentages (+5.23%, -12.4%) and wraps
// them in colored spans. Only signed percentages are colored (see
// backend system prompt - the model is instructed to always sign
// returns) so we never misfire on non-directional numbers like P/E
// ratio or dividend yield, which don't carry a sign.
const SIGNED_PCT = /([+-]\d+\.?\d*%)/g;

function colorize(text) {
  if (typeof text !== "string") return text;
  const parts = text.split(SIGNED_PCT);
  if (parts.length === 1) return text;
  // String.split() with a capturing-group regex deterministically puts
  // the matched groups at ODD indices - no need to re-test the regex
  // (which would be stateful/fragile with the global flag across
  // repeated .test() calls in this loop).
  return parts.map((part, i) => {
    if (i % 2 === 1) {
      const isUp = part.startsWith("+");
      return (
        <span key={i} className={isUp ? "pct-up" : "pct-down"}>
          {part}
        </span>
      );
    }
    return part;
  });
}

// Custom renderers so react-markdown's text nodes get colorized without
// hand-rolling markdown parsing ourselves.
const markdownComponents = {
  strong: ({ children }) => <strong>{Array.isArray(children) ? children.map(colorize) : colorize(children)}</strong>,
  td: ({ children }) => <td>{Array.isArray(children) ? children.map(colorize) : colorize(children)}</td>,
  li: ({ children }) => <li>{Array.isArray(children) ? children.map(colorize) : colorize(children)}</li>,
  p: ({ children }) => <p>{Array.isArray(children) ? children.map(colorize) : colorize(children)}</p>,
};

// Recognized slash commands, transformed into natural-language questions
// before sending - a fast path for common query shapes.
function expandSlashCommand(raw) {
  const trimmed = raw.trim();
  if (!trimmed.startsWith("/")) return raw;

  const compareMatch = trimmed.match(/^\/compare\s+(.+)/i);
  if (compareMatch) {
    const names = compareMatch[1].split(/\s+/).filter(Boolean);
    if (names.length >= 2) return `Compare ${names.join(" and ")} this year`;
  }

  const screenMatch = trimmed.match(/^\/screen\s+(up|down)\s+(\d+(?:\.\d+)?)\s*%?\s*(\S+)?/i);
  if (screenMatch) {
    const [, direction, threshold, period] = screenMatch;
    const periodText = period ? ` over ${period}` : " this month";
    return `Which Nifty50 stocks are ${direction} more than ${threshold}%${periodText}?`;
  }

  const snapshotMatch = trimmed.match(/^\/(?:snapshot|info)\s+(.+)/i);
  if (snapshotMatch) return `Give me a full snapshot of ${snapshotMatch[1].trim()}`;

  return raw; // unrecognized slash command - send as-is, let the model try
}

export default function App() {
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
  const [attachedImage, setAttachedImage] = useState(null); // {base64, mediaType, previewUrl, name}
  const [busy, setBusy] = useState(false);
  const [provider, setProvider] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      return saved?.provider || "anthropic";
    } catch {
      return "anthropic";
    }
  });
  const [answeredBy, setAnsweredBy] = useState(null);
  const [waitingForFirst, setWaitingForFirst] = useState(false);
  const [error, setError] = useState(null);
  const [copiedIndex, setCopiedIndex] = useState(null);
  const [lastQuestion, setLastQuestion] = useState(null);
  const [showAutocomplete, setShowAutocomplete] = useState(false);
  const [showWatchlist, setShowWatchlist] = useState(false);
  const [showGlossary, setShowGlossary] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [showOnboarding, setShowOnboarding] = useState(() => {
    try {
      return !localStorage.getItem(ONBOARDED_KEY);
    } catch {
      return false;
    }
  });
  const [watchlist, setWatchlist] = useState(() => {
    try {
      return JSON.parse(localStorage.getItem(WATCHLIST_KEY) || "[]");
    } catch {
      return [];
    }
  });
  const [watchlistInput, setWatchlistInput] = useState("");

  const bottomRef = useRef(null);
  const abortRef = useRef(null);
  const inputRef = useRef(null);
  const fileInputRef = useRef(null);
  const requestStartRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ sessionId, messages, provider }));
    } catch {
      /* non-fatal */
    }
  }, [sessionId, messages, provider]);

  useEffect(() => {
    try {
      localStorage.setItem(WATCHLIST_KEY, JSON.stringify(watchlist));
    } catch {
      /* non-fatal */
    }
  }, [watchlist]);

  // Jump-to-latest: page-level scroll now (see the flexbox-scroll-bug
  // fix earlier in the project), so "scrolled up" means window scroll
  // position isn't near the bottom of the document.
  useEffect(() => {
    function onScroll() {
      const nearBottom =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 150;
      setShowJumpToLatest(!nearBottom && messages.length > 0);
    }
    window.addEventListener("scroll", onScroll);
    onScroll();
    return () => window.removeEventListener("scroll", onScroll);
  }, [messages.length]);

  // Keyboard shortcuts: Cmd/Ctrl+K focuses input, Escape stops generation,
  // Up-arrow (when input is empty and focused) recalls the last question.
  useEffect(() => {
    function onKeyDown(e) {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        inputRef.current?.focus();
      } else if (e.key === "Escape" && busy) {
        stopGenerating();
      } else if (
        e.key === "ArrowUp" &&
        document.activeElement === inputRef.current &&
        !input &&
        lastQuestion
      ) {
        e.preventDefault();
        setInput(lastQuestion);
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [busy, input, lastQuestion]);

  // Ticker autocomplete: matches the last "word" being typed against
  // ticker symbols and company names.
  const currentWord = input.split(/\s+/).pop() || "";
  const autocompleteMatches = useMemo(() => {
    if (currentWord.length < 2) return [];
    const q = currentWord.toLowerCase();
    return NIFTY50_META.filter(
      (t) => t.ticker.toLowerCase().includes(q) || t.name.toLowerCase().includes(q)
    ).slice(0, 5);
  }, [currentWord]);

  function applyAutocomplete(ticker) {
    const words = input.split(/\s+/);
    words[words.length - 1] = ticker;
    setInput(words.join(" ") + " ");
    setShowAutocomplete(false);
    inputRef.current?.focus();
  }

  async function send(question) {
    const raw = (question ?? input).trim();
    // Allow sending if there's either text OR an attached image
    if ((!raw && !attachedImage) || busy) return;
    const q = expandSlashCommand(raw) || "Please analyze this image.";

    const imageForRequest = attachedImage;

    setInput("");
    setAttachedImage(null);
    setError(null);
    setBusy(true);
    setWaitingForFirst(true);
    setLastQuestion(raw);
    setShowAutocomplete(false);
    setMessages((m) => [...m, {
      role: "user",
      content: raw || "(image)",
      imagePreview: imageForRequest?.previewUrl || null,
    }]);

    const controller = new AbortController();
    abortRef.current = controller;
    requestStartRef.current = performance.now();

    let streamCompleted = false;
    let lastAnsweredBy = null;

    try {
      let res;
      try {
        res = await fetch(`${API}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question: q,
            session_id: sessionId,
            provider,
            image_base64: imageForRequest?.base64 || null,
            image_media_type: imageForRequest?.mediaType || null,
          }),
          signal: controller.signal,
        });
      } catch (e) {
        if (e.name === "AbortError") throw e;
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

          if (event.type === "provider") {
            lastAnsweredBy = { provider: event.provider, model: event.model };
            setAnsweredBy(lastAnsweredBy);
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
                { role: "assistant", content: event.content, meta: null },
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
            const elapsed = requestStartRef.current
              ? Math.round(performance.now() - requestStartRef.current) / 1000
              : null;
            setMessages((m) => {
              const copy = m.map((x) => (x.role === "tool" ? { ...x, running: false } : x));
              const lastIdx = copy.length - 1;
              if (copy[lastIdx]?.role === "assistant") {
                copy[lastIdx] = { ...copy[lastIdx], meta: { ...lastAnsweredBy, latency: elapsed } };
              }
              return copy;
            });
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
    setAnsweredBy(null);
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
  }

  function handleImageSelect(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("Please select an image file (PNG, JPG, etc.).");
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setError("Image too large (max 5MB). Try a smaller screenshot or crop.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      // reader.result is a data URL: "data:image/png;base64,XXXX"
      const dataUrl = reader.result;
      const base64 = dataUrl.split(",")[1];
      setAttachedImage({
        base64,
        mediaType: file.type,
        previewUrl: dataUrl,
        name: file.name,
      });
      setError(null);
    };
    reader.onerror = () => setError("Couldn't read that image file.");
    reader.readAsDataURL(file);
    e.target.value = ""; // reset so selecting the same file again re-triggers
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
      /* clipboard permission denied */
    }
  }

  function addToWatchlist() {
    const t = watchlistInput.trim();
    if (!t) return;
    const ticker = t.toUpperCase().endsWith(".NS") ? t.toUpperCase() : `${t.toUpperCase()}.NS`;
    if (!watchlist.includes(ticker)) setWatchlist((w) => [...w, ticker]);
    setWatchlistInput("");
  }

  function removeFromWatchlist(ticker) {
    setWatchlist((w) => w.filter((t) => t !== ticker));
  }

  function dismissOnboarding() {
    setShowOnboarding(false);
    try {
      localStorage.setItem(ONBOARDED_KEY, "1");
    } catch {
      /* ignore */
    }
  }

  function scrollToLatest() {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  return (
    <div className="app">
      <header className="header">
        <h1>Stock Agent</h1>
        <span className="sub">
          NSE · NIFTY 50 · live via yfinance
          {answeredBy && (
            <span className="answered-by"> · {answeredBy.provider === "ollama" ? "local" : "API"}: {answeredBy.model}</span>
          )}
        </span>
        <div className="header-actions">
          <button
            className="icon-btn"
            onClick={() => setShowGlossary((s) => !s)}
            aria-label="Term definitions"
            title="What do these terms mean?"
          >
            ?
          </button>
          <button
            className="icon-btn"
            onClick={() => setShowWatchlist((s) => !s)}
            aria-label="Watchlist"
            title="Watchlist"
          >
            ★
          </button>
          <div className="provider-toggle" role="radiogroup" aria-label="LLM provider">
            <button
              className={provider === "anthropic" ? "active" : ""}
              onClick={() => setProvider("anthropic")}
              disabled={busy}
              aria-pressed={provider === "anthropic"}
              title="Switching providers starts fresh context for that provider"
            >
              Claude API
            </button>
            <button
              className={provider === "ollama" ? "active" : ""}
              onClick={() => setProvider("ollama")}
              disabled={busy}
              aria-pressed={provider === "ollama"}
              title="Switching providers starts fresh context for that provider"
            >
              Local (free)
            </button>
          </div>
          <button onClick={resetSession}>New session</button>
        </div>
      </header>

      {showGlossary && (
        <div className="panel glossary-panel">
          <div className="panel-header">
            <strong>Term definitions</strong>
            <button className="panel-close" onClick={() => setShowGlossary(false)} aria-label="Close">×</button>
          </div>
          {GLOSSARY.map((g) => (
            <div key={g.term} className="glossary-item">
              <span className="glossary-term">{g.term}</span>
              <span className="glossary-def">{g.def}</span>
            </div>
          ))}
        </div>
      )}

      {showWatchlist && (
        <div className="panel watchlist-panel">
          <div className="panel-header">
            <strong>Watchlist</strong>
            <button className="panel-close" onClick={() => setShowWatchlist(false)} aria-label="Close">×</button>
          </div>
          <div className="watchlist-add">
            <input
              value={watchlistInput}
              onChange={(e) => setWatchlistInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addToWatchlist()}
              placeholder="Add ticker, e.g. RELIANCE"
            />
            <button onClick={addToWatchlist}>Add</button>
          </div>
          {watchlist.length === 0 ? (
            <p className="watchlist-empty">No tickers pinned yet.</p>
          ) : (
            <div className="watchlist-items">
              {watchlist.map((t) => (
                <div key={t} className="watchlist-item">
                  <button
                    className="watchlist-ticker"
                    onClick={() => {
                      send(`Full snapshot of ${t}`);
                      setShowWatchlist(false);
                    }}
                  >
                    {t}
                  </button>
                  <button
                    className="watchlist-remove"
                    onClick={() => removeFromWatchlist(t)}
                    aria-label={`Remove ${t}`}
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

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
          {showOnboarding && (
            <div className="onboarding">
              <p>
                <span className="tool-chip-inline">tool_name args</span> chips show real data being
                fetched live — never guessed. Try <code>/compare TCS INFY</code> or{" "}
                <code>/screen down 10 1M</code> as shortcuts.
              </p>
              <button onClick={dismissOnboarding}>Got it</button>
            </div>
          )}
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
                <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
                  {m.content}
                </ReactMarkdown>
                <div className="msg-footer">
                  <button className="copy-btn" onClick={() => copyMessage(m.content, i)} aria-label="Copy answer">
                    {copiedIndex === i ? "copied" : "copy"}
                  </button>
                  {m.meta && (
                    <span className="msg-meta">
                      {m.meta.provider === "ollama" ? "local" : "API"} · {m.meta.model} · {m.meta.latency}s
                    </span>
                  )}
                </div>
              </div>
            ) : (
              <div key={i} className={`msg ${m.role}`}>
                {m.imagePreview && (
                  <img src={m.imagePreview} alt="uploaded" className="msg-image" />
                )}
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

      {showJumpToLatest && (
        <button className="jump-to-latest" onClick={scrollToLatest}>
          ↓ New messages
        </button>
      )}

      <div className="input-bar-wrap">
        {attachedImage && (
          <div className="attached-preview">
            <img src={attachedImage.previewUrl} alt="attached" />
            <span className="attached-name">{attachedImage.name}</span>
            <button
              className="attached-remove"
              onClick={() => setAttachedImage(null)}
              aria-label="Remove attached image"
            >
              ×
            </button>
          </div>
        )}
        <div className="input-bar">
          <input
            type="file"
            accept="image/*"
            ref={fileInputRef}
            onChange={handleImageSelect}
            style={{ display: "none" }}
          />
          <button
            className="attach-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={busy}
            aria-label="Attach a chart image"
            title="Attach a chart or screenshot"
          >
            +
          </button>
          <div className="input-wrap">
            <input
              ref={inputRef}
              value={input}
              onChange={(e) => {
                setInput(e.target.value);
                setShowAutocomplete(true);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  setShowAutocomplete(false);
                  send();
                }
              }}
              onBlur={() => setTimeout(() => setShowAutocomplete(false), 150)}
              placeholder={attachedImage ? "Ask about this chart… (optional)" : "Ask about any NSE stock… (⌘K to focus, /compare, /screen)"}
              disabled={busy}
              aria-label="Ask a stock question"
            />
            {showAutocomplete && autocompleteMatches.length > 0 && (
              <div className="autocomplete">
                {autocompleteMatches.map((t) => (
                  <button key={t.ticker} onMouseDown={() => applyAutocomplete(t.ticker)}>
                    <span className="ac-ticker">{t.ticker}</span>
                    <span className="ac-name">{t.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {busy ? (
            <button onClick={stopGenerating} className="stop-btn">
              STOP
            </button>
          ) : (
            <button onClick={() => send()} disabled={!input.trim() && !attachedImage}>
              SEND
            </button>
          )}
        </div>
      </div>
    </div>
  );
}