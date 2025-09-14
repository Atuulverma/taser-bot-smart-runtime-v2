import sqlite3
from . import config as C

def _conn():
    return sqlite3.connect(C.DB_PATH)

def init_settings():
    con=_conn(); cur=con.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    con.commit()
    # Seed defaults from .env if missing
    def seed(k, v):
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, str(v)))
    seed("AUTO_TRADING", "true")
    seed("DRY_RUN", "true" if C.DRY_RUN else "false")
    seed("CONSOLE_LOG", "true")
    seed("SCAN_INTERVAL_SECONDS", str(C.SCAN_INTERVAL_SECONDS))
    con.commit(); con.close()

def set_value(key: str, value: str):
    con=_conn(); cur=con.cursor()
    cur.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    con.commit(); con.close()

def get_value(key: str, default: str=None) -> str:
    con=_conn(); cur=con.cursor()
    cur.execute("SELECT value FROM settings WHERE key=?", (key,))
    row=cur.fetchone(); con.close()
    return row[0] if row else default

def as_bool(key: str, default: bool=False)->bool:
    v=get_value(key, "true" if default else "false")
    return str(v).strip().lower() in ("1","true","yes","y","on")

def as_int(key: str, default: int=300)->int:
    v=get_value(key, str(default))
    try:
        return int(v)
    except:
        return default
