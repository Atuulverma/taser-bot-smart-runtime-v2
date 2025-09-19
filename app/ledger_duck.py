from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

try:
    import duckdb
except Exception:
    duckdb = None  # lazy-fail


@dataclass
class TradeOpen:
    trade_id: str
    ts_ms: int
    symbol: str
    side: str
    entry: float
    sl: float
    size_usd: float
    meta: Dict[str, Any]


@dataclass
class TradeClose:
    trade_id: str
    ts_ms: int
    exit: float
    pnl_usd: float
    reason: str
    meta: Dict[str, Any]


def _con(db_path: str):
    if duckdb is None:
        raise RuntimeError("duckdb not installed")
    return duckdb.connect(db_path)


def ensure_schema(db_path: str):
    con = _con(db_path)
    con.execute(
        """
    CREATE TABLE IF NOT EXISTS trades_open(
        trade_id TEXT PRIMARY KEY,
        ts_ms BIGINT,
        symbol TEXT,
        side TEXT,
        entry DOUBLE,
        sl DOUBLE,
        size_usd DOUBLE,
        meta JSON
    );
    """
    )
    con.execute(
        """
    CREATE TABLE IF NOT EXISTS trades_closed(
        trade_id TEXT,
        ts_ms BIGINT,
        exit DOUBLE,
        pnl_usd DOUBLE,
        reason TEXT,
        meta JSON
    );
    """
    )
    con.close()


def append_open(db_path: str, row: TradeOpen):
    con = _con(db_path)
    con.execute(
        "INSERT OR REPLACE INTO trades_open VALUES (?,?,?,?,?,?,?,?);",
        [row.trade_id, row.ts_ms, row.symbol, row.side, row.entry, row.sl, row.size_usd, row.meta],
    )
    con.close()


def append_close(db_path: str, row: TradeClose):
    con = _con(db_path)
    con.execute(
        "INSERT INTO trades_closed VALUES (?,?,?,?,?,?);",
        [row.trade_id, row.ts_ms, row.exit, row.pnl_usd, row.reason, row.meta],
    )
    # remove from open if exists
    con.execute("DELETE FROM trades_open WHERE trade_id = ?;", [row.trade_id])
    con.close()
