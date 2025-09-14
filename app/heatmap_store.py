# app/heatmap_store.py
import os, json, time, sqlite3
from typing import Dict, Any, List, Tuple

from . import config as C

RETENTION_DAYS = int(os.getenv("HEATMAP_RETENTION_DAYS", "90"))
_DB = C.DB_PATH

def _conn():
    return sqlite3.connect(_DB, check_same_thread=False)

def init():
    con=_conn(); cur=con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS heatmap_levels(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts INTEGER,            -- epoch ms
        tf TEXT,               -- '5m' | '15m' | '1h' | '1d' | '30d'
        payload_json TEXT
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_hm_tf_ts ON heatmap_levels(tf, ts)")
    con.commit(); con.close()

def purge_old():
    cutoff = int((time.time() - RETENTION_DAYS*86400) * 1000)
    con=_conn(); cur=con.cursor()
    cur.execute("DELETE FROM heatmap_levels WHERE ts < ?", (cutoff,))
    con.commit(); con.close()

def save_multi(ts_ms: int, hm: Dict[str, Any]):
    if not hm: return
    con=_conn(); cur=con.cursor()
    for tf, payload in hm.items():
        try:
            cur.execute("INSERT INTO heatmap_levels(ts,tf,payload_json) VALUES(?,?,?)",
                        (int(ts_ms), str(tf), json.dumps(payload, default=float)))
        except Exception:
            pass
    con.commit(); con.close()

def recent_levels(tf: str, limit: int = 30) -> List[Dict[str, Any]]:
    con=_conn(); cur=con.cursor()
    cur.execute("""SELECT ts,payload_json FROM heatmap_levels 
                   WHERE tf=? ORDER BY ts DESC LIMIT ?""", (tf, limit))
    rows = [{"ts": r[0], "payload": json.loads(r[1] or "{}")} for r in cur.fetchall()]
    con.close()
    return rows

def _nearest_levels(levels: List[Dict[str, float]], price: float, tol_pct: float) -> Tuple[List[dict], List[dict]]:
    if not levels: return ([], [])
    tol = price * tol_pct
    near = [lv for lv in levels if abs(float(lv["px"]) - price) <= tol]
    above = [lv for lv in near if float(lv["px"]) >= price]
    below = [lv for lv in near if float(lv["px"]) <= price]
    return (above, below)

def confluence_gate(hm_multi: Dict[str, Any], price: float, side: str,
                    tol_pct: float = 0.0015, need_tfs: int = 2, top_n: int = 12) -> Dict[str, Any]:
    """
    If price is within tol_pct of strong walls across >= need_tfs timeframes,
    we block entries in the direction that would run into the wall.
    """
    if not hm_multi: 
        return {"near": False, "block": False, "why": ""}

    tf_keys = [k for k in ["5m","15m","1h","1d","30d"] if k in hm_multi]
    hits_above = 0
    hits_below = 0
    for k in tf_keys:
        levels = (hm_multi.get(k) or {}).get("levels") or []
        levels = levels[:top_n]
        above, below = _nearest_levels(levels, price, tol_pct)
        if above: hits_above += 1
        if below: hits_below += 1

    near = (hits_above + hits_below) > 0
    block = False
    why = ""
    if side.upper() == "LONG" and hits_above >= need_tfs:
        block = True; why = f"near multi-TF resistance ({hits_above} TFs)"
    if side.upper() == "SHORT" and hits_below >= need_tfs:
        block = True; why = f"near multi-TF support ({hits_below} TFs)"
    return {"near": near, "block": block, "why": why, "hits_above": hits_above, "hits_below": hits_below}