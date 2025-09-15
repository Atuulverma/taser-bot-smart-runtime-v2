# app/telemetry.py
import csv
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from . import config as C

# --- Standard telemetry tag constants (for consistency across engines) ---
TEL = {
    "COMP_SCAN": "scan",
    "COMP_EXEC": "exec",
    "COMP_MGR": "manager",
    # tags
    "TAG_STARTUP": "STARTUP",
    "TAG_ENGINE_ORDER": "ENGINE_ORDER",
    "TAG_FILTER_BLOCK": "FILTER_BLOCK",
    "TAG_REVERSE": "REVERSE",
    "TAG_ENTRY_SKIP": "ENTRY_SKIP",
    "TAG_NO_TRADE": "NO_TRADE",
}


def _safe_payload(d: Dict[str, Any] | None) -> Dict[str, Any]:
    try:
        return dict(d or {})
    except Exception:
        return {"_warn": "non-dict payload"}


_lock = threading.Lock()


def _conn():
    # check_same_thread=False so we can log from different async tasks/threads
    con = sqlite3.connect(C.DB_PATH, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("PRAGMA synchronous=NORMAL;")
        con.execute("PRAGMA temp_store=MEMORY;")
        con.execute("PRAGMA mmap_size=134217728;")
    except Exception:
        pass
    return con


def init_telemetry():
    """Create the telemetry table if not exists (idempotent)."""
    try:
        with _lock, _conn() as con:
            cur = con.cursor()
            cur.execute(
                """
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                component TEXT,
                tag TEXT,
                message TEXT,
                payload_json TEXT
            )"""
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_ts ON telemetry(ts)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_comp ON telemetry(component)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_tag ON telemetry(tag)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_cct ON telemetry(component, tag, ts)")
            con.commit()
    except Exception as e:
        print("[TELEMETRY INIT ERROR]", e, flush=True)


def log(component: str, tag: str, message: str, payload: Dict[str, Any] | None = None):
    """Write a telemetry entry. Always safe (catches JSON/DB errors)."""
    try:
        payload_str = json.dumps(payload or {}, default=str)
    except Exception as e:
        payload_str = json.dumps({"_error": f"json:{e}"})

    try:
        with _lock, _conn() as con:
            cur = con.cursor()
            cur.execute(
                "INSERT INTO telemetry(ts,component,tag,message,payload_json) VALUES(?,?,?,?,?)",
                (int(time.time() * 1000), component, tag, message, payload_str),
            )
            con.commit()
    except Exception as e:
        print("[TELEMETRY ERROR]", e, flush=True)


# Structured engine event logger (auto-injects tags for engine/exchange/symbol/trade_id)
def elog(
    component: str,
    tag: str,
    message: str,
    *,
    engine: str | None = None,
    exchange: str | None = None,
    symbol: str | None = None,
    trade_id: int | None = None,
    extra: Dict[str, Any] | None = None,
):
    payload = dict(extra or {})
    if engine is not None:
        payload["engine"] = engine
        payload.setdefault("engine_tag", engine)
    if exchange is not None:
        payload["exchange"] = exchange
    if symbol is not None:
        payload["symbol"] = symbol
    if trade_id is not None:
        payload["trade_id"] = trade_id
    return log(component, tag, message, payload)


def log_exception(component: str, tag: str, exc: Exception, extra: Dict[str, Any] | None = None):
    try:
        info = {
            "type": type(exc).__name__,
            "repr": repr(exc),
        }
        if extra:
            info.update(extra)
        return log(component, tag, "exception", info)
    except Exception:
        return


def recent(limit: int = 100) -> List[dict]:
    """Fetch recent telemetry rows (most recent first)."""
    try:
        with _lock, _conn() as con:
            cur = con.cursor()
            cur.execute(
                "SELECT ts,component,tag,message,payload_json "
                "FROM telemetry ORDER BY id DESC LIMIT ?",
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "ts": r[0],
                "component": r[1],
                "tag": r[2],
                "message": r[3],
                "payload": json.loads(r[4] or "{}"),
            }
            for r in rows
        ]
    except Exception as e:
        print("[TELEMETRY RECENT ERROR]", e, flush=True)
        return []


def purge(older_than_ms: int):
    """Delete telemetry entries older than a given epoch ms (housekeeping)."""
    try:
        with _lock, _conn() as con:
            cur = con.cursor()
            cur.execute("DELETE FROM telemetry WHERE ts < ?", (older_than_ms,))
            con.commit()
    except Exception as e:
        print("[TELEMETRY PURGE ERROR]", e, flush=True)


def recent_filtered(limit: int = 200, component: str = "", q: str = "") -> List[dict]:
    """
    Convenience filter wrapper over recent().
    """
    try:
        rows = recent(limit)
        if component:
            rows = [r for r in rows if r.get("component") == component]
        if q:
            ql = q.lower()
            rows = [
                r
                for r in rows
                if ql in (r.get("message") or "").lower()
                or ql in json.dumps(r.get("payload") or {}).lower()
            ]
        return rows
    except Exception as e:
        print("[TELEMETRY FILTER ERROR]", e, flush=True)
        return []


def recent_by_tag(limit: int = 200, tag: str = "") -> List[dict]:
    """Fetch most recent entries filtered by exact tag."""
    try:
        rows = recent(limit)
        if tag:
            rows = [r for r in rows if r.get("tag") == tag]
        return rows
    except Exception as e:
        print("[TELEMETRY RECENT_BY_TAG ERROR]", e, flush=True)
        return []


# -----------------------------
# Convenience structured logs
# -----------------------------
def log_startup_engine_order(engine_order: list[str]):
    """Emit a heartbeat of the active engine order at startup."""
    try:
        payload = {"engine_order": engine_order}
        return log("runtime", TEL["TAG_STARTUP"], "engine order", payload)
    except Exception:
        return


def log_filter_block(
    engine: str,
    reason: str,
    *,
    exchange: str | None = None,
    symbol: str | None = None,
    filters: Dict[str, Any] | None = None,
):
    """Standardized filter block: why a signal was blocked."""
    payload = _safe_payload(filters)
    payload.update({"engine": engine, "exchange": exchange, "symbol": symbol, "reason": reason})
    return log(TEL["COMP_SCAN"], TEL["TAG_FILTER_BLOCK"], reason, payload)


def log_entry_skip(
    engine: str,
    reason: str,
    *,
    exchange: str | None = None,
    symbol: str | None = None,
    gates: Dict[str, Any] | None = None,
):
    """Entry computed but skipped due to pre-gates; capture booleans/thresholds."""
    payload = _safe_payload(gates)
    payload.update({"engine": engine, "exchange": exchange, "symbol": symbol, "reason": reason})
    return log(TEL["COMP_SCAN"], TEL["TAG_ENTRY_SKIP"], reason, payload)


def log_reverse(
    engine: str,
    allowed: bool,
    *,
    exchange: str | None = None,
    symbol: str | None = None,
    move_r: float | None = None,
    adx: float | None = None,
    ema200_ok: bool | None = None,
    tl_confirm_bars: int | None = None,
    tl_break_atr_mult: float | None = None,
    why: str = "",
):
    """Reverse decision audit: allowed/blocked with context (used by TrendScalp)."""
    payload = {
        "engine": engine,
        "exchange": exchange,
        "symbol": symbol,
        "allowed": bool(allowed),
        "move_r": move_r,
        "adx": adx,
        "ema200_ok": ema200_ok,
        "tl_confirm_bars": tl_confirm_bars,
        "tl_break_atr_mult": tl_break_atr_mult,
        "why": why,
    }
    tag = TEL["TAG_REVERSE"]
    return log(TEL["COMP_MGR"], tag, ("ALLOW" if allowed else "BLOCK"), payload)


IST = timezone(timedelta(hours=5, minutes=30))


def window(
    start_ms: int,
    end_ms: int,
    component: str | None = None,
    tag: str | None = None,
    limit: int = 100000,
) -> List[dict]:
    """Fetch telemetry in a specific window (inclusive start, exclusive end)."""
    try:
        with _lock, _conn() as con:
            cur = con.cursor()
            q = (
                "SELECT ts,component,tag,message,payload_json "
                "FROM telemetry WHERE ts >= ? AND ts < ?"
            )
            args: List[Any] = [start_ms, end_ms]
            if component:
                q += " AND component = ?"
                args.append(component)
            if tag:
                q += " AND tag = ?"
                args.append(tag)
            q += " ORDER BY ts ASC LIMIT ?"
            args.append(limit)
            cur.execute(q, tuple(args))
            rows = cur.fetchall()
        return [
            {
                "ts": r[0],
                "component": r[1],
                "tag": r[2],
                "message": r[3],
                "payload": json.loads(r[4] or "{}"),
            }
            for r in rows
        ]
    except Exception as e:
        print("[TELEMETRY WINDOW ERROR]", e, flush=True)
        return []


def last_hours(hours: int = 24, component: str | None = None, tag: str | None = None) -> List[dict]:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(hours * 3600 * 1000)
    return window(start_ms, now_ms, component, tag)


def export_csv(path: str, rows: List[dict]):
    """Export provided telemetry rows to CSV (UTF-8)."""
    try:
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ts", "ts_ist", "component", "tag", "message", "payload_json"])
            for r in rows:
                ts = int(r.get("ts", 0))
                dt = datetime.fromtimestamp(ts / 1000, tz=IST).strftime("%Y-%m-%d %H:%M:%S")
                writer.writerow(
                    [
                        ts,
                        dt,
                        r.get("component"),
                        r.get("tag"),
                        r.get("message"),
                        json.dumps(r.get("payload") or {}, ensure_ascii=False),
                    ]
                )
    except Exception as e:
        print("[TELEMETRY CSV ERROR]", e, flush=True)


def export_last_24h_csv(path: str, component: str | None = None, tag: str | None = None):
    rows = last_hours(24, component, tag)
    export_csv(path, rows)
