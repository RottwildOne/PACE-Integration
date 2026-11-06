import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    row_hash TEXT NOT NULL UNIQUE,
    run_date TEXT NOT NULL,          -- ISO date
    kind TEXT NOT NULL,              -- dividend | reinvestment | buy | sell
    symbol TEXT NOT NULL,
    description TEXT,
    price REAL,
    quantity REAL,
    amount REAL                      -- cash amount as reported (dividends > 0)
);
CREATE INDEX IF NOT EXISTS idx_tx_symbol ON transactions(symbol);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(run_date);

CREATE TABLE IF NOT EXISTS announced (
    symbol TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    pay_date TEXT,
    record_date TEXT,
    announce_date TEXT,
    amount REAL NOT NULL,            -- per share
    source TEXT NOT NULL DEFAULT 'nasdaq',   -- nasdaq | yahoo
    pay_estimated INTEGER NOT NULL DEFAULT 0, -- 1 when pay_date is a guess
    PRIMARY KEY (symbol, ex_date)
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT PRIMARY KEY,
    price REAL NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings_override (
    symbol TEXT PRIMARY KEY,
    shares REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path):
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after the first release to existing databases."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(announced)")}
    if "source" not in cols:
        conn.execute("ALTER TABLE announced ADD COLUMN source TEXT NOT NULL DEFAULT 'nasdaq'")
    if "pay_estimated" not in cols:
        conn.execute("ALTER TABLE announced ADD COLUMN pay_estimated INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
