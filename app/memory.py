import time, json, sqlite3
from typing import List, Dict
from . import config as C
def _conn(): return sqlite3.connect(C.DB_PATH)
def init_memory_tables():
    con=_conn(); cur=con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS memory_zones(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER, pair TEXT, kind TEXT, payload_json TEXT)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_ts INTEGER, pair TEXT, outcome TEXT,
        entry_price REAL, exit_price REAL, mfe REAL, mae REAL, features_json TEXT, notes TEXT)""")
    con.commit(); con.close()
def store_zone(pair: str, kind: str, payload: Dict):
    con=_conn(); cur=con.cursor()
    cur.execute("INSERT INTO memory_zones(created_ts,pair,kind,payload_json) VALUES(?,?,?,?)",
                (int(time.time()*1000), pair, kind, json.dumps(payload)))
    con.commit(); con.close()
def latest_zones(pair: str, limit:int=5)->List[Dict]:
    con=_conn(); cur=con.cursor()
    cur.execute("SELECT created_ts,kind,payload_json FROM memory_zones WHERE pair=? ORDER BY id DESC LIMIT ?",
                (pair, limit))
    rows=[{"ts":r[0],"kind":r[1],"payload":json.loads(r[2])} for r in cur.fetchall()]
    con.close(); return rows
def store_lesson(pair: str, outcome: str, entry: float, exit_px: float, mfe: float, mae: float, features: Dict, notes: str=""):
    con=_conn(); cur=con.cursor()
    cur.execute("""INSERT INTO lessons(created_ts,pair,outcome,entry_price,exit_price,mfe,mae,features_json,notes)
                   VALUES(?,?,?,?,?,?,?,?,?)""", (int(time.time()*1000), pair, outcome, entry, exit_px, mfe, mae, json.dumps(features), notes))
    con.commit(); con.close()
def recent_lessons(pair: str, limit:int=20)->List[Dict]:
    con=_conn(); cur=con.cursor()
    cur.execute("SELECT created_ts,outcome,entry_price,exit_price,mfe,mae,features_json,notes FROM lessons WHERE pair=? ORDER BY id DESC LIMIT ?",
                (pair, limit))
    rows=[{"ts":r[0],"outcome":r[1],"entry":r[2],"exit":r[3],"mfe":r[4],"mae":r[5],"features":json.loads(r[6]),"notes":r[7]} for r in cur.fetchall()]
    con.close(); return rows
