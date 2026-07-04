"""
SQLite cache for daily OHLCV price data.

Design decision: cache full daily history per ticker, refreshed once a day.
This keeps the agent fast (no live API call on every user question) and
avoids yfinance rate-limiting when screening 50+ tickers at once.
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "db" / "prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume INTEGER,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS cache_meta (
    ticker TEXT PRIMARY KEY,
    last_refreshed TEXT NOT NULL
);
"""


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    return conn


def is_cache_fresh(ticker: str, conn: sqlite3.Connection) -> bool:
    """
    Returns True if this ticker's cache was refreshed today.
    Simple freshness rule for v1: once per calendar day is enough for
    a screener use-case (not a live-quote tool).
    """
    row = conn.execute(
        "SELECT last_refreshed FROM cache_meta WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        return False
    last_refreshed = datetime.fromisoformat(row[0]).date()
    return last_refreshed == date.today()


def save_prices(ticker: str, df, conn: sqlite3.Connection) -> None:
    """df: pandas DataFrame from yfinance with Date index and OHLCV columns."""
    rows = [
        (
            ticker,
            idx.strftime("%Y-%m-%d"),
            float(r["Open"]),
            float(r["High"]),
            float(r["Low"]),
            float(r["Close"]),
            int(r["Volume"]) if not r["Volume"] != r["Volume"] else 0,  # NaN guard
        )
        for idx, r in df.iterrows()
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO prices
           (ticker, date, open, high, low, close, volume)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.execute(
        """INSERT OR REPLACE INTO cache_meta (ticker, last_refreshed)
           VALUES (?, ?)""",
        (ticker, datetime.now().isoformat()),
    )
    conn.commit()


def load_prices(ticker: str, conn: sqlite3.Connection):
    """Returns rows ordered by date ascending, or empty list if none cached."""
    return conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices "
        "WHERE ticker = ? ORDER BY date ASC",
        (ticker,),
    ).fetchall()
