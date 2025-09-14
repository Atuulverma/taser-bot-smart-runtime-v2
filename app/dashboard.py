# app/dashboard.py
from fastapi import FastAPI, Form, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
import sqlite3, json, html, time, datetime
from typing import Any, Dict, List

from . import config as C
from . import telemetry
from . import settings
from . import state
from .data import exchange, fetch_ohlcv, fetch_balance_quote, quote_from_pair
from .analytics import build_liquidity_heatmap
from . import db as DB

app = FastAPI()

# ---------- Utilities ----------
def _fmt_ts(ms: Any) -> str:
    try:
        if not ms:
            return ""
        return datetime.datetime.fromtimestamp(int(ms)/1000).strftime("%d-%m-%y %H:%M:%S")
    except Exception:
        return str(ms)

def _conn():
    con = sqlite3.connect(C.DB_PATH, check_same_thread=False)
    try:
        con.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return con

def _escape(x: Any) -> str:
    if x is None:
        return ""
    return html.escape(str(x), quote=False)

# Helper to check column existence in a table
def _has_column(table: str, col: str) -> bool:
    try:
        con = _conn(); cur = con.cursor()
        cur.execute(f"PRAGMA table_info({table})")
        cols = [c[1] for c in cur.fetchall()]
        con.close()
        return col in cols
    except Exception:
        return False

# ---------- Base HTML shell with page-level refresh controls ----------
def _page_shell(body: str, title: str = "TASER Dashboard") -> str:
    return f"""
    <html>
    <head>
      <title>{_escape(title)}</title>
      <meta name='viewport' content='width=device-width, initial-scale=1'>
      <style>
        body{{font-family:Inter,Arial,Helvetica,sans-serif;margin:0;background:#0b0b0b;color:#ddd}}
        .nav{{display:flex;gap:16px;padding:14px 18px;background:#121212;position:sticky;top:0;border-bottom:1px solid #222}}
        .nav a{{color:#9fef00;text-decoration:none;opacity:0.9}}
        .container{{padding:16px 18px}}
        table{{border-collapse:collapse;width:100%;margin-top:10px}}
        th,td{{border:1px solid #333;padding:6px 8px}} th{{background:#222}}
        .pill{{padding:3px 8px;border-radius:12px;background:#1e1e1e;border:1px solid #333;color:#aaa;font-size:12px}}
        .ok{{color:#9fef00}} .warn{{color:#ffcc00}} .bad{{color:#ff5f5f}}
        input,button{{background:#111;border:1px solid #333;color:#ddd;padding:6px 10px;border-radius:8px}}
        .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}}
        .card{{background:#101010;border:1px solid #222;border-radius:14px;padding:12px}}
        pre{{white-space:pre-wrap;word-break:break-word;background:#0f0f0f;border:1px solid #222;border-radius:10px;padding:10px}}
        .bar{{display:inline-block;height:10px;background:#4da3ff}}
        a.button{{display:inline-block;margin-right:10px}}
        .controls{{display:flex;align-items:center;gap:10px;margin-bottom:8px}}
        .muted{{color:#9aa}}
      </style>
      <script>
        // Generic panel loader with manual + optional auto refresh
        function setupPanel(containerId, endpoint, defaultMs) {{
          let timer = null;
          const wrap = document.getElementById(containerId);
          if (!wrap) return {{ loadOnce: ()=>{{}}, start: ()=>{{}}, stop: ()=>{{}} }};
          const tgt  = wrap.querySelector('[data-target=content]');
          const ts   = wrap.querySelector('[data-target=stamp]');
          const btn  = wrap.querySelector('[data-action=refresh]');
          const chk  = wrap.querySelector('[data-action=auto]');
          const sel  = wrap.querySelector('[data-action=interval]');

          async function loadOnce() {{
            try {{
              const r = await fetch(endpoint, {{ cache: 'no-store' }});
              let j = null; let html = '';
              try {{
                j = await r.json();
                html = j.html || '';
              }} catch(e) {{
                html = await r.text();
              }}
              tgt.innerHTML = html || '';
              if (ts) ts.innerText = new Date().toLocaleTimeString();
            }} catch (e) {{
              tgt.innerHTML = '<div class="card" style="border-color:#533">Fetch error</div>';
            }}
          }}

          function start(ms) {{
            stop();
            timer = setInterval(loadOnce, ms || defaultMs || 5000);
            wrap.dataset.timer = '1';
          }}
          function stop() {{
            if (timer) {{ clearInterval(timer); timer = null; }}
            delete wrap.dataset.timer;
          }}

          if (btn) btn.onclick = () => loadOnce();
          if (chk) chk.onchange = () => {{
            if (chk.checked) start(parseInt((sel && sel.value) || defaultMs || 5000, 10));
            else stop();
          }};
          if (sel) sel.onchange = () => {{
            if (chk && chk.checked) start(parseInt(sel.value || defaultMs || 5000, 10));
          }};

          // initial manual load
          loadOnce();

          return {{ loadOnce, start, stop }};
        }}
      </script>
    </head>
    <body>
      <div class="nav">
        <a href="/">Overview</a>
        <a href="/thinking">Thinking</a>
        <a href="/data">Data</a>
        <a href="/heatmap">Heatmap</a>
        <a href="/positions">Positions</a>
        <a href="/settings">Settings</a>
        <a href="/export">Export</a>
      </div>
      <div class="container">
        {body}
      </div>
    </body>
    </html>
    """

# ---------- OVERVIEW ----------
@app.get("/", response_class=HTMLResponse)
def home():
    body = f"""
      <div id="panel-overview" class="card">
        <div class="controls">
          <button data-action="refresh">Refresh now</button>
          <label><input type="checkbox" data-action="auto"> Auto refresh</label>
          <select data-action="interval">
            <option value="3000">3s</option>
            <option value="5000" selected>5s</option>
            <option value="10000">10s</option>
            <option value="30000">30s</option>
          </select>
          <span class="muted">Last updated: <span data-target="stamp">—</span></span>
        </div>
        <div data-target="content"></div>
      </div>
      <script>setupPanel('panel-overview','/api/overview',5000);</script>
    """
    return HTMLResponse(_page_shell(body, f"TASER — {C.PAIR}"))

@app.get("/api/overview")
def api_overview():
    con=_conn(); cur=con.cursor()
    try:
        cur.execute("""SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,qty,status,created_ts,closed_ts,exit_price,realized_pnl
                       FROM trades ORDER BY id DESC LIMIT 30""")
        trades=cur.fetchall()
        cur.execute("""SELECT ts, tag, note FROM
                       (SELECT trade_id, ts, tag, note FROM events ORDER BY id DESC LIMIT 80) e
                       ORDER BY ts DESC""")
        events=cur.fetchall()
    finally:
        con.close()

    st = state.get()
    html_parts: List[str] = []
    html_parts.append("<div class='grid'>")

    # Status card
    html_parts.append("<div class='card'><h3>Status</h3>")
    html_parts.append(f"<div>Pair: <span class='pill'>{_escape(C.PAIR)}</span></div>")
    html_parts.append(f"<div>Mode: <span class='pill'>{'PAPER' if C.DRY_RUN else 'LIVE'}</span></div>")
    try:
        aut = settings.as_bool("AUTO_TRADING", True)
        dr  = settings.as_bool("DRY_RUN", True)
        log = settings.as_bool("CONSOLE_LOG", True)
        scan = settings.as_int("SCAN_INTERVAL_SECONDS", 300)
    except Exception:
        aut = True; dr = True; log = True; scan = getattr(C, "SCAN_INTERVAL_SECONDS", 300)
    html_parts.append(f"<div>Auto Trading: <span class='pill'>{'ON' if aut else 'OFF'}</span></div>")
    html_parts.append(f"<div>Paper (DRY_RUN): <span class='pill'>{'ON' if dr else 'OFF'}</span></div>")
    html_parts.append(f"<div>Console Log: <span class='pill'>{'ON' if log else 'OFF'}</span></div>")
    html_parts.append(f"<div>Scan Interval: <span class='pill'>{_escape(scan)}</span> s</div>")
    last_price = st.get("last_price")
    html_parts.append(f"<div>Last Price: <b>{_escape(round(last_price,4) if last_price else '—')}</b></div>")
    html_parts.append(f"<div>Orderflow WS: <span class='pill'>{_escape(st.get('ws_status','—'))}</span> | CVD: <b>{_escape(round(st.get('cvd') or 0,2))}</b></div>")
    html_parts.append(f"<div>Last Scan TS: <span class='pill'>{_escape(st.get('last_scan_ts') or '—')}</span></div>")
    if st.get("last_audit"):
        html_parts.append(f"<div>Last Audit: <span class='pill'>{_escape(st['last_audit'].get('decision'))}</span></div>")
    if st.get("errors"):
        errs = "; ".join([e.get("msg","") for e in st.get("errors", [])[-5:]])
        html_parts.append(f"<div style='color:#ff5f5f'>Errors: {_escape(errs)}</div>")
    html_parts.append("</div>")

    # Recovery snapshot (paper account) + avg daily realized PnL and breakeven
    try:
        ex = exchange()
        last_px = st.get("last_price")
        if not last_px:
            try:
                tf1m = fetch_ohlcv(ex, "1m", 2)
                last_px = float(tf1m["close"][-1])
            except Exception:
                last_px = 0.0
        def _f(x, d=0.0):
            try: return float(x)
            except Exception: return d
        def _unreal(side: str, entry: float, px: float, qty: float) -> float:
            return (px - entry) * qty if side and side.upper()=="LONG" else (entry - px) * qty

        # Pull realized PnL and open trades for unrealized from DB
        con=_conn(); cur=con.cursor()
        try:
            cur.execute("""SELECT COALESCE(SUM(realized_pnl),0) FROM trades WHERE realized_pnl IS NOT NULL""")
            realized_pnl_total = _f(cur.fetchone()[0], 0.0)
            cur.execute("""SELECT id,symbol,side,entry,qty,status FROM trades WHERE status IN ('OPEN','PARTIAL')""")
            open_trades = cur.fetchall()
        finally:
            con.close()

        unreal = 0.0
        for _, _sym, _side, _entry, _qty, _status in open_trades:
            unreal += _unreal(_side, _f(_entry), _f(last_px), _f(_qty))

        # Paper start balance from settings (fallback to live or default)
        DB.init_settings()
        quote_ccy = quote_from_pair(C.PAIR)
        paper_start = DB.get_setting("paper_start_balance", None)
        if paper_start is None:
            try:
                live_quote = fetch_balance_quote(ex, C.PAIR)
            except Exception:
                live_quote = None
            amt = float(live_quote) if live_quote is not None else 1000.0
            paper_start = {"ccy": quote_ccy, "amount": amt}
            DB.set_setting("paper_start_balance", paper_start)

        start_amt = _f(paper_start.get("amount"), 0.0)
        equity = start_amt + realized_pnl_total + unreal
        dd_abs = max(0.0, start_amt - equity)
        dd_pct = (dd_abs / start_amt * 100.0) if start_amt > 0 else 0.0
        rec_pct = (equity / start_amt * 100.0) if start_amt > 0 else 0.0

        # Paper sizing mode indicator (uses config flags)
        paper_sizing = "START BAL" if (getattr(C, "DRY_RUN", True) and getattr(C, "PAPER_USE_START_BALANCE", False)) else ("EQUITY" if getattr(C, "DRY_RUN", True) else "LIVE")

        # Avg daily realized PnL from settings
        avg_daily_realized_pnl = DB.get_setting("avg_daily_realized_pnl", None)
        avg_daily_realized_pnl_val = None
        try:
            avg_daily_realized_pnl_val = float(avg_daily_realized_pnl)
        except Exception:
            avg_daily_realized_pnl_val = None

        # Compute days to breakeven (to recover drawdown)
        days_to_breakeven = None
        if avg_daily_realized_pnl_val and avg_daily_realized_pnl_val > 0 and dd_abs > 0:
            days_to_breakeven = dd_abs / avg_daily_realized_pnl_val
        elif avg_daily_realized_pnl_val and avg_daily_realized_pnl_val > 0:
            days_to_breakeven = 0

        html_parts.append("<div class='card'><h3>Recovery</h3>")
        html_parts.append(f"<div>Paper Sizing Mode: <span class='pill'>{_escape(paper_sizing)}</span></div>")
        html_parts.append(f"<div>Start: <b>{_escape(f'{start_amt:.2f} {paper_start.get('ccy', quote_ccy)}')}</b></div>")
        html_parts.append(f"<div>Realized PnL: <b>{_escape(f'{realized_pnl_total:.2f}')}</b> | Unrealized: <b>{_escape(f'{unreal:.2f}')}</b></div>")
        html_parts.append(f"<div>Equity: <b>{_escape(f'{equity:.2f} {paper_start.get('ccy', quote_ccy)}')}</b></div>")
        html_parts.append(f"<div>Drawdown: <b class='{'bad' if dd_abs>0 else 'ok'}'>{_escape(f'${dd_abs:.2f} ({dd_pct:.2f}%)')}</b> | Recovered: <b>{_escape(f'{rec_pct:.2f}%')}</b></div>")
        html_parts.append("<hr>")
        html_parts.append("<div><b>Avg Daily Realized PnL</b>: "
            f"<span id='avg-pnl-val'>{_escape(f'{avg_daily_realized_pnl_val:.2f}' if avg_daily_realized_pnl_val is not None else '—')}</span>"
            "</div>")
        if days_to_breakeven is not None:
            html_parts.append(f"<div>Days to Breakeven: <b>{_escape(f'{days_to_breakeven:.1f}')}</b></div>")
        html_parts.append(
            """
            <form id='avg-pnl-form' style='margin-top:6px;display:flex;gap:6px;align-items:center'
                  onsubmit="event.preventDefault(); (async () => {
                    const amt = document.getElementById('avg-pnl-input').value;
                    const btn = document.getElementById('avg-pnl-btn');
                    btn.disabled = true;
                    try {
                      const r = await fetch('/settings/set-avg-pnl', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                        body: 'amount=' + encodeURIComponent(amt)
                      });
                      const j = await r.json();
                      if (j.ok) {
                        document.getElementById('avg-pnl-val').innerText = parseFloat(amt).toFixed(2);
                        alert('Saved avg daily PnL');
                        location.reload();
                      } else {
                        alert(j.msg || 'Failed');
                      }
                    } catch(e) { alert('Failed'); }
                    btn.disabled = false;
                  })();">
                <label for='avg-pnl-input'>Set Avg Daily Realized PnL:</label>
                <input id='avg-pnl-input' name='amount' type='number' step='0.01' min='0' style='width:90px'
                       value='""" + (f"{avg_daily_realized_pnl_val:.2f}" if avg_daily_realized_pnl_val is not None else "") + """'>
                <button id='avg-pnl-btn' type='submit'>Save</button>
            </form>
            """
        )
        html_parts.append("</div>")
    except Exception as _e:
        html_parts.append("<div class='card' style='border-color:#553'><b>Recovery:</b> error computing snapshot</div>")

    # Engine PnL (24h & 7d) — PAPER only with toggle
    try:
        now_ms = int(time.time()*1000)
        cutoff_24h = now_ms - 24*3600*1000
        cutoff_7d  = now_ms - 7*24*3600*1000
        con=_conn(); cur=con.cursor()
        use_account = _has_column('trades','account')
        use_engine  = _has_column('trades','engine')

        def _fetch_engine_rows(cutoff):
            if use_engine:
                if use_account:
                    cur.execute(
                        """
                        SELECT COALESCE(engine,'taser') as eng,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account = 'PAPER'
                        GROUP BY eng
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
                else:
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
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
            else:
                if use_account:
                    cur.execute(
                        """
                        SELECT COALESCE(json_extract(meta_json,'$.engine'),'taser') as eng,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account = 'PAPER'
                        GROUP BY eng
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
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
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
            return cur.fetchall()

        rows24 = _fetch_engine_rows(cutoff_24h)
        rows7  = _fetch_engine_rows(cutoff_7d)
        con.close()

        def _render_engine_table(rows):
            if not rows:
                return "<div class='muted'>No closed trades in window.</div>"
            max_pnl = max((float(r[4] or 0.0) for r in rows), default=0.0)
            max_px = 260
            html = ["<table><tr><th>Engine</th><th>Trades</th><th>W/L</th><th>Total PnL</th><th></th></tr>"]
            for r in rows:
                eng  = str(r[0] or "?")
                trd  = int(r[1] or 0)
                wins = int(r[2] or 0)
                loss = int(r[3] or 0)
                pnl  = float(r[4] or 0.0)
                w = 1
                if max_pnl > 0:
                    try:
                        w = int((pnl / max_pnl) * max_px)
                    except Exception:
                        w = 1
                bar = f"<span class='bar' style='width:{w}px'></span>"
                cls = 'ok' if pnl > 0 else ('bad' if pnl < 0 else '')
                html.append(
                    f"<tr><td><span class='pill'>{_escape(eng.upper())}</span></td>"
                    f"<td>{trd}</td><td>{wins}/{loss}</td>"
                    f"<td class='{cls}'>{pnl:.2f}</td><td>{bar}</td></tr>"
                )
            html.append("</table>")
            return "".join(html)

        # Card with toggle and CSV links
        html_parts.append("<div class='card'><h3>Engine PnL — Paper</h3>")
        html_parts.append(
            "<div class='controls'>"
            "<button id='btn-pnl-24h'>24h</button>"
            "<button id='btn-pnl-7d'>7d</button>"
            "<a class='button' href='/export/engine_pnl.csv?window=24h'>CSV 24h</a>"
            "<a class='button' href='/export/engine_pnl.csv?window=7d'>CSV 7d</a>"
            "</div>"
        )
        html_parts.append("<div id='engine-24h'>" + _render_engine_table(rows24) + "</div>")
        html_parts.append("<div id='engine-7d' style='display:none'>" + _render_engine_table(rows7) + "</div>")
        html_parts.append("<script>\n"
                          "(function(){\n"
                          "  const e24=document.getElementById('engine-24h');\n"
                          "  const e7=document.getElementById('engine-7d');\n"
                          "  const b24=document.getElementById('btn-pnl-24h');\n"
                          "  const b7=document.getElementById('btn-pnl-7d');\n"
                          "  if(b24) b24.onclick=()=>{e24.style.display=''; e7.style.display='none';};\n"
                          "  if(b7) b7.onclick=()=>{e7.style.display=''; e24.style.display='none';};\n"
                          "})();\n"
                          "</script>")
        html_parts.append("</div>")
    except Exception:
        html_parts.append("<div class='card' style='border-color:#553'><b>Engine PnL:</b> error computing</div>")

    # Exchange PnL (24h & 7d) — PAPER with toggle and CSV links
    try:
        now_ms = int(time.time()*1000)
        cutoff_24h = now_ms - 24*3600*1000
        cutoff_7d = now_ms - 7*24*3600*1000
        con=_conn(); cur=con.cursor()
        use_account = _has_column('trades','account')
        use_exchange= _has_column('trades','exchange')

        def _fetch_exchange_rows(cutoff):
            if use_exchange:
                if use_account:
                    cur.execute(
                        """
                        SELECT COALESCE(exchange,'delta') as exch,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                        GROUP BY exch
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(exchange,'delta') as exch,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                        GROUP BY exch
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
            else:
                if use_account:
                    cur.execute(
                        """
                        SELECT COALESCE(json_extract(meta_json,'$.exchange'),'delta') as exch,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                        GROUP BY exch
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
                else:
                    cur.execute(
                        """
                        SELECT COALESCE(json_extract(meta_json,'$.exchange'),'delta') as exch,
                               COUNT(1) as trades,
                               SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                               SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                               COALESCE(SUM(realized_pnl),0)
                        FROM trades
                        WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                        GROUP BY exch
                        ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                        """, (cutoff,)
                    )
            return cur.fetchall()

        rows_24h = _fetch_exchange_rows(cutoff_24h)
        rows_7d = _fetch_exchange_rows(cutoff_7d)
        con.close()

        def _render_exchange_table(rows):
            if not rows:
                return "<div class='muted'>No closed trades in window.</div>"
            html = ["<table><tr><th>Exchange</th><th>Trades</th><th>W/L</th><th>Total PnL</th></tr>"]
            for r in rows:
                exch = str(r[0] or "?")
                trd  = int(r[1] or 0)
                wins = int(r[2] or 0)
                loss = int(r[3] or 0)
                pnl  = float(r[4] or 0.0)
                cls = 'ok' if pnl > 0 else ('bad' if pnl < 0 else '')
                html.append(
                    f"<tr><td><span class='pill'>{_escape(exch.upper())}</span></td>"
                    f"<td>{trd}</td><td>{wins}/{loss}</td><td class='{cls}'>{pnl:.2f}</td></tr>"
                )
            html.append("</table>")
            return "".join(html)

        html_parts.append("<div class='card'><h3>Exchange PnL — Paper</h3>")
        html_parts.append(
            "<div class='controls'>"
            "<button id='btn-exch-pnl-24h'>24h</button>"
            "<button id='btn-exch-pnl-7d'>7d</button>"
            "<a class='button' href='/export/exchange_pnl.csv?window=24h'>CSV 24h</a>"
            "<a class='button' href='/export/exchange_pnl.csv?window=7d'>CSV 7d</a>"
            "</div>"
        )
        html_parts.append("<div id='exch-pnl-24h'>" + _render_exchange_table(rows_24h) + "</div>")
        html_parts.append("<div id='exch-pnl-7d' style='display:none'>" + _render_exchange_table(rows_7d) + "</div>")
        html_parts.append(
            "<script>\n"
            "(function(){\n"
            "  const e24=document.getElementById('exch-pnl-24h');\n"
            "  const e7=document.getElementById('exch-pnl-7d');\n"
            "  const b24=document.getElementById('btn-exch-pnl-24h');\n"
            "  const b7=document.getElementById('btn-exch-pnl-7d');\n"
            "  if(b24) b24.onclick=()=>{e24.style.display=''; e7.style.display='none';};\n"
            "  if(b7) b7.onclick=()=>{e7.style.display=''; e24.style.display='none';};\n"
            "})();\n"
            "</script>"
        )
        html_parts.append("</div>")
    except Exception:
        html_parts.append("<div class='card' style='border-color:#553'><b>Exchange PnL:</b> error computing</div>")

    # Last signal
    html_parts.append("<div class='card'><h3>Last Signal</h3>")
    ls = st.get("last_signal")
    if ls:
        html_parts.append(f"<div>Side: <b>{_escape(ls.get('side'))}</b> | Entry: {_escape(ls.get('entry'))} | SL: {_escape(ls.get('sl'))}</div>")
        html_parts.append(f"<div>TPs: {_escape(ls.get('tps'))}</div>")
        html_parts.append(f"<div>Reason: {_escape(ls.get('reason'))}</div>")
    else:
        html_parts.append("<div>— No signal yet</div>")
    html_parts.append("</div>")

    # Trades table
    html_parts.append("<div class='card' style='grid-column:1/-1'><h3>Recent Trades</h3>"
                      "<table><tr><th>ID</th><th>Side</th><th>Entry</th><th>SL</th><th>TPs</th>"
                      "<th>Qty</th><th>Status</th><th>Created</th><th>Closed</th><th>Exit</th><th>PnL</th></tr>")
    for t in trades:
        _id, sym, side, entry, sl, tp1, tp2, tp3, qty, status, cts, ets, exitp, pnl = t
        sl_txt = f"{sl:.4f}" if sl is not None else ""
        tp_txt = ",".join([x for x in [str(tp1), str(tp2), str(tp3)] if x not in ("None","")])
        exit_txt = "" if exitp is None else f"{exitp:.4f}"
        pnl_txt = "" if pnl is None else f"{pnl:.2f}"
        html_parts.append(
            f"<tr><td>{_escape(_id)}</td><td>{_escape(side)}</td><td>{_escape(f'{entry:.4f}')}</td>"
            f"<td>{_escape(sl_txt)}</td><td>{_escape(tp_txt)}</td><td>{_escape(qty)}</td>"
            f"<td>{_escape(status)}</td><td>{_fmt_ts(cts)}</td><td>{_fmt_ts(ets) if ets else ''}</td>"
            f"<td>{_escape(exit_txt)}</td><td>{_escape(pnl_txt)}</td></tr>"
        )
    html_parts.append("</table></div>")

    # Events table
    html_parts.append("<div class='card' style='grid-column:1/-1'><h3>Events</h3>"
                      "<table><tr><th>TS</th><th>Tag</th><th>Note</th></tr>")
    for ts, tag, note in events:
        html_parts.append(f"<tr><td>{_fmt_ts(ts)}</td><td>{_escape(tag)}</td><td>{_escape(note)}</td></tr>")
    html_parts.append("</table></div>")

    html_parts.append("</div>")
    return JSONResponse({"html": "".join(html_parts)})

# ---------- POSITIONS ----------
@app.get("/positions", response_class=HTMLResponse)
def positions_page():
    body = """
      <div id="panel-positions" class="card">
        <div class="controls">
          <button data-action="refresh">Refresh now</button>
          <label><input type="checkbox" data-action="auto"> Auto refresh</label>
          <select data-action="interval">
            <option value="4000" selected>4s</option>
            <option value="7000">7s</option>
            <option value="15000">15s</option>
          </select>
          <span class="muted">Last updated: <span data-target="stamp">—</span></span>
        </div>
        <div data-target="content"></div>
      </div>
      <script>setupPanel('panel-positions','/api/positions',4000);</script>
    """
    return HTMLResponse(_page_shell(body, "Positions & Balance"))

@app.post("/positions/reset-paper-start")
def reset_paper_start():
    ex = exchange()
    quote = quote_from_pair(C.PAIR)
    try:
        live_quote = fetch_balance_quote(ex, C.PAIR)
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"Wallet fetch error: {e}"}, status_code=status.HTTP_400_BAD_REQUEST)

    from . import db as DB
    DB.init_settings()
    if live_quote is None:
        return JSONResponse(
            {"ok": False, "msg": "Live balance not available; check API keys/permissions."},
            status_code=status.HTTP_400_BAD_REQUEST
        )
    DB.set_setting("paper_start_balance", {"ccy": quote, "amount": float(live_quote)})
    return JSONResponse({"ok": True, "msg": f"Paper start set to {live_quote:.2f} {quote}"})

@app.get("/api/positions")
def api_positions():
    ex = exchange()

    def _f(x, d=0.0):
        try: return float(x)
        except Exception: return d

    def _unreal(side: str, entry: float, px: float, qty: float) -> float:
        return (px - entry) * qty if side and side.upper()=="LONG" else (entry - px) * qty

    # last price
    try:
        tf1m = fetch_ohlcv(ex, "1m", 2)
        last_px = _f(tf1m["close"][-1], 0.0)
    except Exception:
        last_px = 0.0

    # live wallet / positions
    quote_ccy = quote_from_pair(C.PAIR)
    banner = ""
    try:
        live_quote = fetch_balance_quote(ex, C.PAIR)
    except Exception as e:
        live_quote = None
        banner = f"<div class='card' style='border-color:#553'><b>Note:</b> Wallet fetch error: {str(e)}</div>"

    try:
        positions = ex.fetch_positions([C.PAIR]) if hasattr(ex, "fetch_positions") else []
    except Exception:
        positions = []
        if not banner:
            banner = "<div class='card' style='border-color:#553'><b>Note:</b> fetch_positions unavailable or creds missing.</div>"

    live_rows = ""
    for p in positions or []:
        side = (p.get("side") or ("long" if _f(p.get("contracts"))>0 else "short")).upper()
        qty  = _f(p.get("contracts") or p.get("positionAmt"))
        entry= _f(p.get("entryPrice") or p.get("entry_price"))
        u    = _unreal(side, entry, last_px, qty)
        live_rows += (
            f"<tr><td>{_escape(side)}</td><td>{_escape(qty)}</td>"
            f"<td>{_escape(f'{entry:.4f}' if entry else '')}</td>"
            f"<td>{_escape(f'{last_px:.4f}' if last_px else '')}</td>"
            f"<td>{_escape(f'{u:.2f}')}</td></tr>"
        )
    if not live_rows:
        live_rows = "<tr><td colspan='5'>No live positions</td></tr>"
    live_bal_html = (f"<b>{live_quote:.2f} {quote_ccy}</b>" if live_quote is not None else "<i>n/a</i>")

    # DB open trades + realized (paper)
    con=_conn(); cur=con.cursor()
    try:
        cur.execute("""SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,qty,status,created_ts
                       FROM trades WHERE status IN ('OPEN','PARTIAL') ORDER BY id DESC""")
        open_trades = cur.fetchall()
        cur.execute("""SELECT COALESCE(SUM(realized_pnl),0) FROM trades
                       WHERE realized_pnl IS NOT NULL""")
        realized_pnl_total = _f(cur.fetchone()[0], 0.0)
    finally:
        con.close()

    # Initialize / read paper start
    from . import db as DB
    DB.init_settings()
    paper_start = DB.get_setting("paper_start_balance", None)
    if paper_start is None:
        if live_quote is not None:
            DB.set_setting("paper_start_balance", {"ccy": quote_ccy, "amount": float(live_quote)})
            paper_start = {"ccy": quote_ccy, "amount": float(live_quote)}
        else:
            DB.set_setting("paper_start_balance", {"ccy": quote_ccy, "amount": 1000.0})
            paper_start = {"ccy": quote_ccy, "amount": 1000.0}

    # Paper unrealized
    unreal = 0.0
    paper_rows = ""
    for t in open_trades:
        tid, sym, side, entry, sl, tp1, tp2, tp3, qty, status, cts = t
        u = _unreal(side, _f(entry), last_px, _f(qty))
        unreal += u
        paper_rows += (
            f"<tr><td>{_escape(tid)}</td><td>{_escape(side)}</td>"
            f"<td>{_escape(f'{_f(entry):.4f}' if entry else '')}</td>"
            f"<td>{_escape(_f(qty))}</td><td>{_escape(status)}</td>"
            f"<td>{_escape(f'{last_px:.4f}' if last_px else '')}</td>"
            f"<td>{_escape(f'{u:.2f}')}</td></tr>"
        )
    if not paper_rows:
        paper_rows = "<tr><td colspan='7'>No open paper trades</td></tr>"

    paper_equity = float(paper_start["amount"]) + realized_pnl_total + unreal
    reset_btn = """
      <div class='card'>
        <h3>Paper Controls</h3>
        <button onclick="(async () => {
          try {
            const r = await fetch('/positions/reset-paper-start', {method:'POST'});
            const j = await r.json();
            alert(j.msg || (j.ok ? 'Reset done' : 'Reset failed'));
            try { location.reload(); } catch(e) {}
          } catch(e) { alert('Reset failed'); }
        })()">Reset Paper Start = Live Balance</button>
      </div>
    """
    html_body = f"""
      {banner}
      <div class='grid'>
        {reset_btn}
        <div class='card'>
          <h3>Live Wallet</h3>
          <div>Quote Balance: {live_bal_html}</div>
          <div>Last Price: <b>{_escape(f'{last_px:.4f}' if last_px else 'n/a')}</b></div>
        </div>
        <div class='card'>
          <h3>Live Positions ({_escape(C.PAIR)})</h3>
          <table><tr><th>Side</th><th>Qty</th><th>Entry</th><th>Mark</th><th>Unrealized</th></tr>
            {live_rows}
          </table>
        </div>
        <div class='card'>
          <h3>Paper Account</h3>
          <div>Start: <b>{_escape(f"{paper_start['amount']:.2f} {paper_start['ccy']}")}</b></div>
          <div>Realized PnL: <b>{_escape(f'{realized_pnl_total:.2f}')}</b></div>
          <div>Unrealized PnL: <b>{_escape(f'{unreal:.2f}')}</b></div>
          <div>Equity: <b>{_escape(f'{paper_equity:.2f} {paper_start["ccy"]}')}</b></div>
        </div>
        <div class='card' style='grid-column:1/-1'>
          <h3>Open Paper Trades (DB)</h3>
          <table>
            <tr><th>ID</th><th>Side</th><th>Entry</th><th>Qty</th><th>Status</th><th>Mark</th><th>Unrealized</th></tr>
            {paper_rows}
          </table>
        </div>
      </div>
    """
    return JSONResponse({"html": html_body})

# ---------- THINKING (Telemetry) ----------
@app.get("/thinking", response_class=HTMLResponse)
def thinking_page():
    body = """
      <div class="card">
        <form id="tform" onsubmit="event.preventDefault(); applyFilter();">
          <input id='component' placeholder='component=scan/audit/exec/surveil/orderflow' />
          <input id='q' placeholder='search text…' />
          <button type='submit'>Filter</button>
        </form>
      </div>
      <div id="panel-thinking" class="card">
        <div class="controls">
          <button data-action="refresh">Refresh now</button>
          <label><input type="checkbox" data-action="auto"> Auto refresh</label>
          <select data-action="interval">
            <option value="2000" selected>2s</option>
            <option value="5000">5s</option>
            <option value="10000">10s</option>
          </select>
          <span class="muted">Last updated: <span data-target="stamp">—</span></span>
        </div>
        <div data-target="content"></div>
      </div>
      <script>
        let panel;
        let endpoint = '/api/telemetry';

        function buildEndpoint() {
          const c = document.getElementById('component').value || '';
          const q = document.getElementById('q').value || '';
          const params = new URLSearchParams();
          if (c) params.set('component', c);
          if (q) params.set('q', q);
          return '/api/telemetry' + (params.toString() ? ('?' + params.toString()) : '');
        }

        function applyFilter() {
          endpoint = buildEndpoint();
          // rebuild panel with the new endpoint once, then manual load
          panel = setupPanel('panel-thinking', endpoint, 2000);
          panel.loadOnce();
        }

        document.addEventListener('DOMContentLoaded', () => {
          // first mount
          panel = setupPanel('panel-thinking', endpoint, 2000);
          panel.loadOnce();
        });
      </script>
    """
    return HTMLResponse(_page_shell(body, "Thinking Feed"))

@app.get("/api/telemetry")
def api_telemetry(component: str = "", q: str = "", limit: int = 200):
    # Try best-effort to support both recent(limit=) and recent()
    try:
        rows = telemetry.recent_filtered(limit=limit, component=component, q=q)  # type: ignore
    except Exception:
        try:
            rows = telemetry.recent(limit=limit)  # type: ignore
        except TypeError:
            rows = telemetry.recent()  # type: ignore
            rows = rows[-limit:]
        if component:
            rows = [r for r in rows if r.get("component") == component]
        if q:
            ql = q.lower()
            def _in(r):
                try:
                    payload_txt = json.dumps(r.get("payload") or {}, default=str).lower()
                except Exception:
                    payload_txt = str(r.get("payload") or "").lower()
                return ql in (r.get("message") or "").lower() or ql in payload_txt
            rows = [r for r in rows if _in(r)]

    tr = []
    for r in rows:
        try:
            payload = json.dumps(r.get("payload") or {}, default=str)[:800]
        except Exception:
            payload = str(r.get("payload") or "")[:800]
        payload = payload.replace("<", "&lt;").replace(">", "&gt;")
        tr.append(
            f"<tr><td>{_fmt_ts(r.get('ts'))}</td><td>{_escape(r.get('component'))}</td>"
            f"<td>{_escape(r.get('tag'))}</td><td>{_escape(r.get('message'))}</td>"
            f"<td><pre>{payload}</pre></td></tr>"
        )
    html_table = "<table><tr><th>TS</th><th>Component</th><th>Tag</th><th>Message</th><th>Payload</th></tr>" + "".join(tr) + "</table>"
    return JSONResponse({"html": html_table, "count": len(rows)})

# ---------- DATA ----------
@app.get("/data", response_class=HTMLResponse)
def data_page():
    body = """
      <div id="panel-data" class="card">
        <div class="controls">
          <button data-action="refresh">Refresh now</button>
          <label><input type="checkbox" data-action="auto"> Auto refresh</label>
          <select data-action="interval">
            <option value="4000" selected>4s</option>
            <option value="7000">7s</option>
            <option value="15000">15s</option>
          </select>
          <span class="muted">Last updated: <span data-target="stamp">—</span></span>
        </div>
        <div data-target="content"></div>
      </div>
      <script>setupPanel('panel-data','/api/data',4000);</script>
    """
    return HTMLResponse(_page_shell(body, "Data & Features"))

@app.get("/api/data")
def api_data():
    ex = exchange()
    tf1m = fetch_ohlcv(ex, "1m", 200)
    tf5  = fetch_ohlcv(ex, "5m", 500)
    tf15 = fetch_ohlcv(ex, "15m", 500)
    tf1h = fetch_ohlcv(ex, "1h", 240)
    def lastN(tf, n):
        return {k:(v[-n:] if isinstance(v, list) else v) for k,v in tf.items()}
    html_body = "<div class='grid'>"
    html_body += "<div class='card'><h3>1m (last 50)</h3><pre>"+_escape(json.dumps(lastN(tf1m,50), indent=2)[:4000])+"</pre></div>"
    html_body += "<div class='card'><h3>5m (last 80)</h3><pre>"+_escape(json.dumps(lastN(tf5,80), indent=2)[:4000])+"</pre></div>"
    html_body += "<div class='card'><h3>15m (last 80)</h3><pre>"+_escape(json.dumps(lastN(tf15,80), indent=2)[:4000])+"</pre></div>"
    html_body += "<div class='card'><h3>1h (last 80)</h3><pre>"+_escape(json.dumps(lastN(tf1h,80), indent=2)[:4000])+"</pre></div>"
    html_body += "</div>"
    return JSONResponse({"html": html_body})

# ---------- HEATMAP ----------
@app.get("/heatmap", response_class=HTMLResponse)
def heatmap_page():
    body = """
      <div id="panel-heatmap" class="card">
        <div class="controls">
          <button data-action="refresh">Refresh now</button>
          <label><input type="checkbox" data-action="auto"> Auto refresh</label>
          <select data-action="interval">
            <option value="7000" selected>7s</option>
            <option value="15000">15s</option>
            <option value="30000">30s</option>
          </select>
          <span class="muted">Last updated: <span data-target="stamp">—</span></span>
        </div>
        <div data-target="content"></div>
      </div>
      <script>setupPanel('panel-heatmap','/api/heatmap',7000);</script>
    """
    return HTMLResponse(_page_shell(body, "Heatmap"))

@app.get("/api/heatmap")
def api_heatmap():
    ex = exchange()
    tf5  = fetch_ohlcv(ex, "5m",  800)
    tf15 = fetch_ohlcv(ex, "15m", 800)

    hm5  = build_liquidity_heatmap(tf5,  window=180) or {"levels": []}
    hm15 = build_liquidity_heatmap(tf15, window=180) or {"levels": []}

    def render_panel(title: str, levels: list, show_all=False):
        rows = levels if show_all else levels[:24]
        max_score = max((lv["score"] for lv in rows), default=1.0)
        max_px = 280
        htmlp = [f"<div class='card'><h3>{title}</h3>"]
        for lv in rows:
            px = lv["px"]
            sc = float(lv["score"])
            w  = int((sc / max_score) * max_px) if max_score > 0 else 1
            htmlp.append(
                "<div style='display:flex;align-items:center;gap:10px;margin:3px 0'>"
                f"<span class='pill' style='min-width:70px;text-align:right'>{px}</span>"
                f"<span class='bar' style='width:{w}px'></span>"
                f"<span style='font-size:12px;color:#8aa'>{sc:.0f}</span>"
                "</div>"
            )
        htmlp.append("</div>")
        return "".join(htmlp)

    html = "<div class='grid' style='grid-template-columns:1fr 1fr 1fr 1fr'>"
    # 5m (Top, Full)
    html += render_panel("5m — Top Levels", hm5.get("levels", []), show_all=False)
    html += render_panel("5m — Full Histogram", hm5.get("levels", []), show_all=True)
    # 15m (Top, Full)
    html += render_panel("15m — Top Levels", hm15.get("levels", []), show_all=False)
    html += render_panel("15m — Full Histogram", hm15.get("levels", []), show_all=True)
    html += "</div>"

    return JSONResponse({"html": html})

# ---------- SETTINGS ----------
@app.get("/settings", response_class=HTMLResponse)
def view_settings():
    s_aut = settings.as_bool("AUTO_TRADING", True)
    s_dry = settings.as_bool("DRY_RUN", True)
    s_log = settings.as_bool("CONSOLE_LOG", True)
    s_int = settings.as_int("SCAN_INTERVAL_SECONDS", 300)
    body = f"""
    <form method='post' action='/settings' class='card'>
      <h3>Runtime Settings</h3>
      <label>Auto Trading:</label> <input type='checkbox' name='AUTO_TRADING' {"checked" if s_aut else ""}><br>
      <label>Paper Trade (DRY_RUN):</label> <input type='checkbox' name='DRY_RUN' {"checked" if s_dry else ""}><br>
      <label>Console Logging:</label> <input type='checkbox' name='CONSOLE_LOG' {"checked" if s_log else ""}><br>
      <label>Scan Interval (sec):</label> <input type='number' name='SCAN_INTERVAL_SECONDS' value='{_escape(s_int)}'><br><br>
      <div class='card' style='margin-top:10px'>
        <h4>Arm to Go Live</h4>
        <div>If you are switching from PAPER to LIVE, type <code>ARM LIVE</code> to confirm.</div>
        <input name='CONFIRM' placeholder='ARM LIVE'>
      </div>
      <br>
      <button type='submit'>Save</button>
    </form>
    """
    return HTMLResponse(_page_shell(body, "Settings"))

@app.post("/settings")
def save_settings(AUTO_TRADING: str = Form(None),
                  DRY_RUN: str = Form(None),
                  CONSOLE_LOG: str = Form(None),
                  SCAN_INTERVAL_SECONDS: int = Form(...),
                  CONFIRM: str = Form("")):
    prev_dry = settings.as_bool("DRY_RUN", True)
    new_dry = (DRY_RUN is not None)
    if prev_dry and not new_dry:
        if (CONFIRM or "").strip().upper() != "ARM LIVE":
            return HTMLResponse(_page_shell(
                "<h3 style='color:#ff5f5f'>Confirmation text missing. Type 'ARM LIVE' to go live.</h3>", "Settings"
            ))
    settings.set_value("AUTO_TRADING", "true" if AUTO_TRADING is not None else "false")
    settings.set_value("DRY_RUN", "true" if new_dry else "false")
    settings.set_value("CONSOLE_LOG", "true" if CONSOLE_LOG is not None else "false")
    settings.set_value("SCAN_INTERVAL_SECONDS", str(SCAN_INTERVAL_SECONDS))
    return HTMLResponse(_page_shell("<h3>Saved ✅</h3>", "Settings"))

# Endpoint to set avg daily realized PnL for breakeven calc
@app.post("/settings/set-avg-pnl")
def set_avg_pnl(amount: str = Form("")):
    try:
        v = float(amount)
    except Exception:
        return JSONResponse({"ok": False, "msg": "Invalid number"}, status_code=status.HTTP_400_BAD_REQUEST)
    DB.init_settings()
    DB.set_setting("avg_daily_realized_pnl", v)
    return JSONResponse({"ok": True, "msg": f"Saved avg daily PnL = {v:.2f}"})

# ---------- EXPORT ----------
# ---------- EXPORT ----------

# Engine PnL export endpoint
# Engine PnL export endpoint
@app.get("/export/engine_pnl.csv")
def export_engine_pnl_csv(window: str = "24h"):
    import csv, io
    win = (window or "24h").lower()
    hours = 24 if win == "24h" else (24*7 if win == "7d" else 24)
    cutoff = int(time.time()*1000) - hours*3600*1000

    con=_conn(); cur=con.cursor()
    use_account = _has_column('trades','account')
    use_engine  = _has_column('trades','engine')

    if use_engine:
        if use_account:
            cur.execute(
                """
                SELECT COALESCE(engine,'taser') as eng,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                GROUP BY eng
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
        else:
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
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
    else:
        if use_account:
            cur.execute(
                """
                SELECT COALESCE(json_extract(meta_json,'$.engine'),'taser') as eng,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                GROUP BY eng
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
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
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
    rows = cur.fetchall(); con.close()

    buf = io.StringIO(); writer = csv.writer(buf)
    writer.writerow(["engine","trades","wins","losses","total_pnl","window_hours"])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], float(r[4] or 0.0), hours])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")

# Exchange PnL export endpoint
@app.get("/export/exchange_pnl.csv")
def export_exchange_pnl_csv(window: str = "24h"):
    import csv, io
    win = (window or "24h").lower()
    hours = 24 if win == "24h" else (24*7 if win == "7d" else 24)
    cutoff = int(time.time()*1000) - hours*3600*1000
    con = _conn(); cur = con.cursor()
    use_account = _has_column('trades','account')
    use_exchange = _has_column('trades','exchange')
    if use_exchange:
        if use_account:
            cur.execute(
                """
                SELECT COALESCE(exchange,'delta') as exch,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                GROUP BY exch
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
        else:
            cur.execute(
                """
                SELECT COALESCE(exchange,'delta') as exch,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                GROUP BY exch
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
    else:
        if use_account:
            cur.execute(
                """
                SELECT COALESCE(json_extract(meta_json,'$.exchange'),'delta') as exch,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ? AND account='PAPER'
                GROUP BY exch
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
        else:
            cur.execute(
                """
                SELECT COALESCE(json_extract(meta_json,'$.exchange'),'delta') as exch,
                       COUNT(1) as trades,
                       SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                       COALESCE(SUM(realized_pnl),0)
                FROM trades
                WHERE closed_ts IS NOT NULL AND closed_ts >= ?
                GROUP BY exch
                ORDER BY COALESCE(SUM(realized_pnl),0) DESC
                """, (cutoff,)
            )
    rows = cur.fetchall(); con.close()
    buf = io.StringIO(); writer = csv.writer(buf)
    writer.writerow(["exchange","trades","wins","losses","total_pnl","window_hours"])
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], float(r[4] or 0.0), hours])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")

@app.get("/export", response_class=HTMLResponse)
def export_page():
    body = """
      <div class='card'>
        <h3>Exports</h3>
        <ul>
          <li><a class='button' href='/export/telemetry.csv'>Download Telemetry CSV</a></li>
          <li><a class='button' href='/export/trades.csv'>Download Trades CSV</a></li>
          <li><a class='button' href='/export/events.csv'>Download Events CSV</a></li>
        </ul>
      </div>
    """
    return HTMLResponse(_page_shell(body, "Export"))

@app.get("/export/telemetry.csv")
def export_telemetry_csv():
    import csv, io
    try:
        rows = telemetry.recent(limit=10000)
    except TypeError:
        rows = telemetry.recent()
        rows = rows[-10000:]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "component", "tag", "message", "payload"])
    for r in rows:
        try:
            payload = json.dumps(r.get("payload") or {}, default=str)
        except Exception:
            payload = str(r.get("payload") or "")
        writer.writerow([
            r.get("ts"),
            r.get("component"),
            r.get("tag"),
            (r.get("message") or "").replace("\n", " "),
            payload
        ])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv")

@app.get("/export/trades.csv")
def export_trades_csv():
    con=_conn(); cur=con.cursor()
    try:
        cur.execute("""SELECT id,symbol,side,entry,sl,tp1,tp2,tp3,qty,status,created_ts,closed_ts,exit_price,realized_pnl
                       FROM trades ORDER BY id DESC""")
        rows=cur.fetchall()
    finally:
        con.close()
    out = "id,symbol,side,entry,sl,tp1,tp2,tp3,qty,status,created_ts,closed_ts,exit_price,realized_pnl\n" + \
          "\n".join([",".join([str(x) for x in r]) for r in rows])
    return PlainTextResponse(out, media_type="text/csv")

@app.get("/export/events.csv")
def export_events_csv():
    con=_conn(); cur=con.cursor()
    try:
        cur.execute("SELECT trade_id,ts,tag,note FROM events ORDER BY id DESC")
        rows=cur.fetchall()
    finally:
        con.close()
    out = "trade_id,ts,tag,note\n" + "\n".join([",".join([str(x) for x in r]) for r in rows])
    return PlainTextResponse(out, media_type="text/csv")