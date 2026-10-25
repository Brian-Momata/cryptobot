"""SQLite state: ledger, decisions, orders and equity samples.

The exchange is the source of truth for balances. This file is the bot's memory of what it
*meant* to do, so the two can be reconciled.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    trade_date TEXT, candle_time TEXT, target REAL, current REAL, price REAL,
    action TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    client_id TEXT PRIMARY KEY, trade_date TEXT, side TEXT, status TEXT,
    requested REAL, base_delta REAL, quote_delta REAL, avg_price REAL, fee_quote REAL,
    mainnet_open REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS equity (ts TEXT, equity REAL, position REAL, price REAL);
"""


class State:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---- key/value ----
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key: str, value) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def delete(self, key: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ---- records ----
    def add_decision(self, **row) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO decisions VALUES (:trade_date, :candle_time, :target, :current, :price,"
                " :action, :reason, :created_at)", row)

    def add_order(self, client_id: str, trade_date: str, side: str, requested: float, created_at: str) -> None:
        """Written BEFORE the order is sent, so a crash mid-send leaves a trace to resolve."""
        with self.db:
            self.db.execute(
                "INSERT INTO orders (client_id, trade_date, side, status, requested, created_at)"
                " VALUES (?, ?, ?, 'pending', ?, ?)", (client_id, trade_date, side, requested, created_at))

    def fill_order(self, client_id: str, base_delta: float, quote_delta: float, avg_price: float,
                   fee_quote: float, mainnet_open: float | None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE orders SET status='filled', base_delta=?, quote_delta=?, avg_price=?, fee_quote=?,"
                " mainnet_open=? WHERE client_id=?",
                (base_delta, quote_delta, avg_price, fee_quote, mainnet_open, client_id))

    def mark_order(self, client_id: str, status: str) -> None:
        with self.db:
            self.db.execute("UPDATE orders SET status=? WHERE client_id=?", (status, client_id))

    def orders(self, status: str | None = None, trade_date: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM orders WHERE 1=1", []
        if status:
            q, args = q + " AND status = ?", args + [status]
        if trade_date:
            q, args = q + " AND trade_date = ?", args + [trade_date]
        return [dict(r) for r in self.db.execute(q + " ORDER BY rowid", args)]

    def decisions(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM decisions ORDER BY rowid")]

    def add_equity(self, ts: str, equity: float, position: float, price: float) -> None:
        with self.db:
            self.db.execute("INSERT INTO equity VALUES (?, ?, ?, ?)", (ts, equity, position, price))
