import json
import sqlite3
import time
from typing import Optional

from . import config as C


def init():
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS trades(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT, side TEXT, entry REAL, sl REAL, tp1 REAL, tp2 REAL, tp3 REAL,
        qty REAL, status TEXT, created_ts INTEGER, closed_ts INTEGER,
        exit_price REAL, realized_pnl REAL, meta_json TEXT
    )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER, order_id TEXT, type TEXT, side TEXT,
        price REAL, qty REAL, status TEXT, created_ts INTEGER
    )"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS events(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER, ts INTEGER, tag TEXT, note TEXT
    )"""
    )
    # Ensure optional enrichment columns exist
    try:
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
    except Exception:
        cols = []
    # Add account/engine/exchange columns if missing
    if "account" not in cols:
        try:
            cur.execute("ALTER TABLE trades ADD COLUMN account TEXT")
        except Exception:
            pass
    if "engine" not in cols:
        try:
            cur.execute("ALTER TABLE trades ADD COLUMN engine TEXT")
        except Exception:
            pass
    if "exchange" not in cols:
        try:
            cur.execute("ALTER TABLE trades ADD COLUMN exchange TEXT")
        except Exception:
            pass
    con.commit()
    con.close()


def exec(sql, params=()):
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    cur.execute(sql, params)
    con.commit()
    lid = cur.lastrowid
    con.close()
    return lid


def query(sql, params=()):
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    cur.execute(sql, params)
    rows = cur.fetchall()
    con.close()
    return rows


def new_trade(symbol, side, entry, sl, tps, qty, meta):
    now = int(time.time() * 1000)
    tps = tps or []
    tp1, tp2, tp3 = (tps + [None, None, None])[:3]
    # derive labels
    try:
        engine = (meta or {}).get("engine", "taser")
        exchange = (meta or {}).get("exchange", "delta")
    except Exception:
        engine, exchange = "taser", "delta"
    account = "PAPER" if getattr(C, "DRY_RUN", True) else "LIVE"

    # discover columns so we can include optional fields if present
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        use_account = "account" in cols
        use_engine = "engine" in cols
        use_exchange = "exchange" in cols
        # build INSERT dynamically to avoid migration breakage
        base_cols = [
            "symbol",
            "side",
            "entry",
            "sl",
            "tp1",
            "tp2",
            "tp3",
            "qty",
            "status",
            "created_ts",
            "closed_ts",
            "exit_price",
            "realized_pnl",
            "meta_json",
        ]
        base_vals = [
            symbol,
            side,
            entry,
            sl,
            tp1,
            tp2,
            tp3,
            qty,
            "OPEN",
            now,
            None,
            None,
            None,
            json.dumps(meta),
        ]
        extra_cols, extra_vals = [], []
        if use_account:
            extra_cols.append("account")
            extra_vals.append(account)
        if use_engine:
            extra_cols.append("engine")
            extra_vals.append(engine)
        if use_exchange:
            extra_cols.append("exchange")
            extra_vals.append(exchange)
        all_cols = base_cols + extra_cols
        placeholders = ",".join(["?"] * len(all_cols))
        sql = f"INSERT INTO trades({','.join(all_cols)}) VALUES({placeholders})"
        cur.execute(sql, base_vals + extra_vals)
        tid = cur.lastrowid
        con.commit()
    finally:
        con.close()
    exec(
        "INSERT INTO events(trade_id,ts,tag,note) VALUES(?,?,?,?)",
        (tid, now, "NEW_TRADE", f"{side} @ {entry} | SL {sl} | TPs {tps}"),
    )
    return tid


def update_status(tid: int, status: str, note: str = ""):
    now = int(time.time() * 1000)
    exec("UPDATE trades SET status=? WHERE id=?", (status, tid))
    exec(
        "INSERT INTO events(trade_id,ts,tag,note) VALUES(?,?,?,?)",
        (tid, now, status, note),
    )


def close_trade(tid: int, exit_px: float, pnl: float, tag: str):
    now = int(time.time() * 1000)
    exec(
        "UPDATE trades SET status=?, closed_ts=?, exit_price=?, realized_pnl=? WHERE id=?",
        (tag, now, exit_px, pnl, tid),
    )
    exec(
        "INSERT INTO events(trade_id,ts,tag,note) VALUES(?,?,?,?)",
        (tid, now, "CLOSED", f"{tag} @ {exit_px}, PnL {pnl}"),
    )


def add_order(tid: int, oid: str, typ: str, side: str, price: float, qty: float, status: str):
    now = int(time.time() * 1000)
    exec(
        """INSERT INTO orders(trade_id,order_id,type,side,price,qty,status,created_ts)
            VALUES(?,?,?,?,?,?,?,?)""",
        (tid, oid, typ, side, price, qty, status, now),
    )
    # --- add near your existing init/create functions ---


def init_market_tables():
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,
        pair TEXT,
        kind TEXT,              -- 'scan' or 'signal'
        payload_json TEXT
    )"""
    )
    con.commit()
    con.close()


def save_snapshot(ts_ms: int, pair: str, kind: str, payload: dict):
    import json
    import sqlite3

    try:
        con = sqlite3.connect(C.DB_PATH)
        cur = con.cursor()
        cur.execute(
            "INSERT INTO market_snapshots(ts,pair,kind,payload_json) VALUES(?,?,?,?)",
            (int(ts_ms), pair, kind, json.dumps(payload, default=str)),
        )
        con.commit()
        con.close()
    except Exception as e:
        print("[DB SNAPSHOT ERROR]", e)


def has_open_trade() -> bool:
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("SELECT COUNT(1) FROM trades WHERE status IN ('OPEN','PARTIAL')")
        n = cur.fetchone()[0]
        return n > 0
    finally:
        con.close()


def get_open_trade():
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            """SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,qty,status,created_ts
                       FROM trades WHERE status IN ('OPEN','PARTIAL')
                       ORDER BY id DESC LIMIT 1"""
        )
        row = cur.fetchone()
        return row
    finally:
        con.close()


def update_trade_status(
    trade_id: int,
    status: str,
    exit_price: Optional[float] = None,
    pnl: Optional[float] = None,
) -> None:
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        if status in ("CLOSED", "STOPPED"):
            cur.execute(
                """UPDATE trades SET status=?, closed_ts=?, exit_price=?, realized_pnl=?
                           WHERE id=?""",
                (status, int(time.time() * 1000), exit_price, pnl, trade_id),
            )
        else:
            cur.execute("UPDATE trades SET status=? WHERE id=?", (status, trade_id))
        con.commit()
    finally:
        con.close()


def append_event(trade_id: int, tag: str, note: str = ""):
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            """INSERT INTO events(trade_id,ts,tag,note)
                       VALUES(?,?,?,?)""",
            (trade_id, int(time.time() * 1000), tag, note),
        )
        con.commit()
    finally:
        con.close()


def save_partial_fill(trade_id: int, which_tp: str, px: float, qty_filled: float):
    # optional: create a partials table if you don’t have it
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            """CREATE TABLE IF NOT EXISTS partials(
                         id INTEGER PRIMARY KEY AUTOINCREMENT,
                         trade_id INTEGER, ts INTEGER,
                         leg TEXT, price REAL, qty REAL)"""
        )
        cur.execute(
            """INSERT INTO partials(trade_id,ts,leg,price,qty)
                       VALUES(?,?,?,?,?)""",
            (trade_id, int(time.time() * 1000), which_tp, px, qty_filled),
        )
        con.commit()
    finally:
        con.close()


# === settings KV (for paper_start, etc.) ===
def init_settings():
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            ts INTEGER
        )
    """
    )
    con.commit()
    con.close()


def get_setting(key: str, default=None):
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("SELECT value FROM settings WHERE key=?", (key,))
        r = cur.fetchone()
        if r and r[0] is not None:
            try:
                return json.loads(r[0])
            except Exception:
                return r[0]
        return default
    finally:
        con.close()


def set_setting(key: str, value):
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            "INSERT OR REPLACE INTO settings(key,value,ts) VALUES(?,?,?)",
            (key, json.dumps(value), int(time.time() * 1000)),
        )
        con.commit()
    finally:
        con.close()


# =========================
# Trade account tagging (PAPER / LIVE)
# =========================
def ensure_trades_account_column():
    """Add 'account' column to trades if it doesn't exist."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        if "account" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN account TEXT")
            con.commit()
    finally:
        con.close()


def ensure_trades_engine_exchange_columns():
    """Add 'engine' and 'exchange' columns to trades if they don't exist."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        if "engine" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN engine TEXT")
        if "exchange" not in cols:
            cur.execute("ALTER TABLE trades ADD COLUMN exchange TEXT")
        con.commit()
    finally:
        con.close()


def tag_trade_account(trade_id: int, account: str):
    """Set the account label on a trade ('PAPER' or 'LIVE')."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("UPDATE trades SET account=? WHERE id=?", (account, trade_id))
        con.commit()
    finally:
        con.close()


def list_open_trades():
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            """SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,qty,created_ts
                       FROM trades WHERE status IN ('OPEN','PARTIAL')
                       ORDER BY id ASC"""
        )
        rows = cur.fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "id": r[0],
                    "symbol": r[1],
                    "side": r[2],
                    "entry": float(r[3]),
                    "sl": float(r[4]),
                    "tp1": r[5],
                    "tp2": r[6],
                    "tp3": r[7],
                    "qty": float(r[8]),
                    "created_ts": int(r[9] or 0),
                }
            )
        return out
    finally:
        con.close()


def get_trade_engine(trade_id: int) -> str:
    """Return the engine string for a trade id.
    Prefers the dedicated 'engine' column if present; otherwise falls back to meta_json.engine.
    Defaults to 'taser' if unavailable.
    """
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        # Check table schema to decide whether the dedicated column exists
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        if "engine" in cols:
            cur.execute("SELECT COALESCE(engine,'taser') FROM trades WHERE id=?", (trade_id,))
            r = cur.fetchone()
            if r and r[0] is not None:
                return str(r[0])
        # Fallback to meta_json
        try:
            sql = (
                "SELECT COALESCE(json_extract(meta_json, '$.engine'), 'taser') "
                "FROM trades WHERE id=?"
            )
            cur.execute(sql, (trade_id,))
            r = cur.fetchone()
            if r and r[0] is not None:
                return str(r[0])
        except Exception:
            pass
        return "taser"
    finally:
        con.close()


def engine_split_pnl(hours: int = 24):
    """Return aggregated PnL by engine for the last N hours.
    Output: List of dicts: {engine, trades, wins, losses, total_pnl}
    """
    cutoff = int(time.time() * 1000) - int(hours * 3600 * 1000)
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        # Ensure engine column exists; if not, derive engine from meta_json at query time
        cur.execute("PRAGMA table_info(trades)")
        cols = [c[1] for c in cur.fetchall()]
        has_engine = "engine" in cols
        if has_engine:
            cur.execute(
                """
                SELECT COALESCE(engine,'taser') as eng,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                GROUP BY eng
                """,
                (cutoff,),
            )
        else:
            cur.execute(
                """
                SELECT COALESCE(json_extract(meta_json,'$.engine'),'taser') as eng,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                GROUP BY eng
                """,
                (cutoff,),
            )
        rows = cur.fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "engine": r[0],
                    "trades": int(r[1] or 0),
                    "wins": int(r[2] or 0),
                    "losses": int(r[3] or 0),
                    "total_pnl": float(r[4] or 0.0),
                }
            )
        return out
    finally:
        con.close()


# === Partial fill helpers ===
def get_trade(trade_id: int):
    """Return a single trade row as a dict (or None)."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute(
            """
            SELECT
                id, symbol, side, entry, sl,
                tp1, tp2, tp3, qty, status,
                created_ts, closed_ts, exit_price, realized_pnl,
                meta_json
            FROM trades
            WHERE id=?
            """,
            (trade_id,),
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "id": r[0],
            "symbol": r[1],
            "side": r[2],
            "entry": float(r[3]),
            "sl": float(r[4]),
            "tp1": r[5],
            "tp2": r[6],
            "tp3": r[7],
            "qty": float(r[8] if r[8] is not None else 0.0),
            "status": r[9],
            "created_ts": int(r[10] or 0),
            "closed_ts": int(r[11] or 0) if r[11] is not None else None,
            "exit_price": float(r[12]) if r[12] is not None else None,
            "realized_pnl": float(r[13]) if r[13] is not None else None,
            "meta_json": r[14],
        }
    finally:
        con.close()


def get_trade_qty(trade_id: int) -> float:
    """Return current recorded qty for a trade (0.0 if missing)."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("SELECT COALESCE(qty,0.0) FROM trades WHERE id=?", (trade_id,))
        r = cur.fetchone()
        return float(r[0] if r else 0.0)
    finally:
        con.close()


def update_trade_qty_and_status(trade_id: int, new_qty: float, status: Optional[str] = None):
    """Update trades.qty and (optionally) status within a single transaction."""
    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        if status is None:
            cur.execute("UPDATE trades SET qty=? WHERE id=?", (float(new_qty), trade_id))
        else:
            cur.execute(
                "UPDATE trades SET qty=?, status=? WHERE id=?",
                (float(new_qty), status, trade_id),
            )
        con.commit()
    finally:
        con.close()


def reduce_trade_qty(
    trade_id: int,
    qty_filled: float,
    price: Optional[float] = None,
    leg: str = "TP1_FILL",
) -> float:
    """
    Atomically reduce a trade's recorded qty and mark as PARTIAL if remainder > 0.
    If remainder becomes 0, leave status management to the caller (manager will close).
    Also writes an events row and a 'partials' row (via save_partial_fill).
    Returns the new remaining qty.
    """
    if qty_filled is None or qty_filled <= 0:
        return get_trade_qty(trade_id)

    con = sqlite3.connect(C.DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "SELECT COALESCE(qty,0.0), COALESCE(status,'OPEN') FROM trades WHERE id=?",
            (trade_id,),
        )
        r = cur.fetchone()
        if not r:
            cur.execute("ROLLBACK")
            return 0.0
        curr_qty = float(r[0] or 0.0)
        new_qty = max(0.0, curr_qty - float(qty_filled))
        # keep existing status if flat; manager finalizes later
        new_status = "PARTIAL" if new_qty > 0.0 else r[1]
        cur.execute("UPDATE trades SET qty=?, status=? WHERE id=?", (new_qty, new_status, trade_id))
        # event log
        now = int(time.time() * 1000)
        remain_str = f"{new_qty:.6f}"
        price_str = _price if (_price := (price if price is not None else "n/a")) else "n/a"
        note = f"{leg}: filled {qty_filled:.6f} @ {price_str}, remain {remain_str}"
        cur.execute(
            "INSERT INTO events(trade_id,ts,tag,note) VALUES(?,?,?,?)",
            (trade_id, now, leg, note),
        )
        con.commit()
    finally:
        con.close()

    try:
        # also store partial detail row (best-effort)
        save_partial_fill(
            trade_id,
            leg,
            float(price) if price is not None else 0.0,
            float(qty_filled),
        )
    except Exception:
        pass

    return new_qty
