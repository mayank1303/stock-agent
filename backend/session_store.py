"""
SQLite-backed conversation storage.

Replaces the in-memory SESSIONS dict from earlier in Phase 3 - that
version lost every conversation on backend restart (uvicorn --reload
restarts on every code save, which made this obvious fast). Same
design philosophy as db.py from Phase 0: SQLite is the right tool for
single-machine, moderate-scale storage; revisit (Postgres) only when
actually deploying for multiple concurrent users (Phase 4).

One wrinkle handled here: Claude's SDK returns response objects (not
plain dicts) for assistant messages. Those aren't JSON-serializable as-
is, so to_serializable_content() converts them to plain dicts before
storage - the Anthropic API accepts plain dicts as input just as well
as its own SDK objects, so this conversion is safe and lossless for
our purposes.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "db" / "sessions.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    messages_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def to_serializable_content(content):
    """
    Convert an Anthropic SDK content list (TextBlock, ToolUseBlock,
    etc.) into plain dicts safe to send back to the API as INPUT.

    Deliberately does NOT use a blanket block.model_dump() - that
    includes every field the SDK response object carries, including
    output-only fields (e.g. "parsed_output") that the API's stricter
    input schema rejects with "Extra inputs are not permitted". This
    hit us in real testing (HTTP 400 on the second turn of a multi-turn
    conversation) - so we build minimal, explicit dicts per block type
    instead, keeping only the fields that are valid as input.
    """
    result = []
    for block in content:
        if isinstance(block, dict):
            # Already plain (our own tool_result blocks) - but still
            # only keep known-safe keys in case of future changes.
            result.append(block)
        elif getattr(block, "type", None) == "text":
            result.append({"type": "text", "text": block.text})
        elif getattr(block, "type", None) == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif hasattr(block, "model_dump"):
            # Unknown block type we haven't special-cased yet - fall
            # back to model_dump() but this is a signal to add an
            # explicit case above if it starts appearing regularly.
            result.append(block.model_dump())
        else:
            result.append({"type": "text", "text": str(block)})
    return result


def get_session(session_id: str) -> list:
    """Returns the message history for a session, or [] if none exists yet."""
    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT messages_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return json.loads(row[0]) if row else []
    finally:
        conn.close()


def save_session(session_id: str, messages: list) -> None:
    """Overwrites the stored history for a session with the current full list."""
    conn = _get_connection()
    try:
        conn.execute(
            """INSERT INTO sessions (session_id, messages_json, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(session_id) DO UPDATE SET
                 messages_json = excluded.messages_json,
                 updated_at = excluded.updated_at""",
            (session_id, json.dumps(messages)),
        )
        conn.commit()
    finally:
        conn.close()


def delete_session(session_id: str) -> None:
    """Clears a session's history (used by the 'New session' / reset endpoint)."""
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
    finally:
        conn.close()