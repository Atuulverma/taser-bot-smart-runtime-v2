# app/data.py
import ccxt
import json
import time
from typing import Any, Dict, List, Optional
from . import config as C

# --- Suggested minimum bars per timeframe (override via config/env) ---
_DEF_MIN_BARS = {
    "1m":  300,   # 5h of 1m
    "3m":  300,   # 15h of 3m
    "5m":  240,   # 20h of 5m
    "15m": 240,   # 2.5d of 15m
    "30m": 240,
    "1h":  240,   # 10d of 1h
}

# derive min bars from engines' needs + config overrides
def _min_bars_for(tf: str) -> int:
    tf = str(tf).lower()
    # engine-driven needs (lookbacks)
    tl_len   = int(getattr(C, "TF_TL_LOOKBACK", getattr(C, "TS_TL_LOOKBACK", 14)))
    ema_slow = int(max(getattr(C, "TF_EMA_SLOW", 20), getattr(C, "TS_EMA_SLOW", 20)))
    # small safety buffer for regressions/filters
    need = max(tl_len, ema_slow, 20) + 20
    # base per-tf default
    base = _DEF_MIN_BARS.get(tf, 240)
    # config/env overrides like OHLCV_MIN_BARS_5M=300
    key = f"OHLCV_MIN_BARS_{tf.replace('m','M').replace('h','H')}"
    override = int(getattr(C, key, 0) or 0)
    return max(need, override if override > 0 else base)

# Optional dep: requests (for REST fallback)
try:
    import requests
except Exception:
    requests = None  # we'll guard against this

# ---------------------------
# Exchange factory
# ---------------------------
def exchange():
    ex_id = getattr(C, "EXCHANGE_ID", "delta").lower()
    if ex_id == "delta":
        ex = ccxt.delta({
            "apiKey": getattr(C, "DELTA_API_KEY", None),
            "secret": getattr(C, "DELTA_API_SECRET", None),
            "enableRateLimit": True,
        })
        base = (getattr(C, "DELTA_BASE_URL", "") or "").rstrip("/")
        if base:
            # CCXT delta uses dict for urls["api"] with public/private
            ex.urls["api"] = {"public": base, "private": base}
        return ex
    return getattr(ccxt, ex_id)()

# ---------------------------
# Normalizers / helpers
# ---------------------------
def _empty_dict() -> Dict[str, List[float]]:
    return {"timestamp": [], "open": [], "high": [], "low": [], "close": [], "volume": []}

def _normalize_ohlcv_rows_to_dict(rows: Any) -> Dict[str, List[float]]:
    """
    Accepts:
      - CCXT-style list[[ms, o, h, l, c, v], ...]
      - Dicts with keys like result/data/candles -> list[dict | list]
    Returns dict of arrays.
    Ensures timestamps are in **milliseconds**.
    """
    # If "rows" already a list of lists/tuples (CCXT typical)
    if isinstance(rows, list) and (not rows or isinstance(rows[0], (list, tuple))):
        out = _empty_dict()
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) < 5:
                continue
            t = int(r[0])
            # normalize to ms
            ts_ms = t if t >= 10**12 else t * 1000
            o, h, l, c = float(r[1]), float(r[2]), float(r[3]), float(r[4])
            v = float(r[5]) if len(r) > 5 else 0.0
            out["timestamp"].append(ts_ms)
            out["open"].append(o)
            out["high"].append(h)
            out["low"].append(l)
            out["close"].append(c)
            out["volume"].append(v)
        return out

    # If "rows" is a dict, unwrap common containers then normalize
    if isinstance(rows, dict):
        for key in ("result", "data", "candles", "items"):
            inner = rows.get(key)
            if inner is None:
                continue
            # list[dict] or list[list]
            if isinstance(inner, list) and inner and isinstance(inner[0], dict):
                out = _empty_dict()
                for it in inner:
                    t = it.get("time") or it.get("timestamp") or it.get("ts") or it.get("t")
                    o = it.get("open") or it.get("o")
                    h = it.get("high") or it.get("h")
                    l = it.get("low") or it.get("l")
                    c = it.get("close") or it.get("c")
                    v = it.get("volume") or it.get("v") or 0
                    if None in (t, o, h, l, c):
                        raise ValueError(f"Unrecognized OHLCV dict row: {it}")
                    t = int(t)
                    ts_ms = t if t >= 10**12 else t * 1000
                    out["timestamp"].append(ts_ms)
                    out["open"].append(float(o))
                    out["high"].append(float(h))
                    out["low"].append(float(l))
                    out["close"].append(float(c))
                    out["volume"].append(float(v))
                return out
            if isinstance(inner, list):
                # assume list of arrays
                return _normalize_ohlcv_rows_to_dict(inner)

        # no recognized container keys
        raise ValueError(f"Unexpected OHLCV dict shape: {json.dumps(rows)[:300]}")

    raise ValueError(f"Unexpected OHLCV type: {type(rows)}")

# Delta REST support
_TIMEFRAME_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "1d": 86400, "7d": 604800, "30d": 2592000, "1w": 604800, "2w": 1209600,
}
_MAX_CANDLES = 2000

def _delta_rest_fetch(tf: str, limit: int, *, symbol: Optional[str] = None) -> Dict[str, List[float]]:
    """
    Calls Delta India (or region) REST using the base URL from config:
      C.DELTA_BASE_URL (e.g., https://api.india.delta.exchange)
    Endpoint: /v2/history/candles
    Params: resolution=tf, symbol=..., start=sec, end=sec
    Returns dict-of-arrays with timestamps in **milliseconds**.
    """
    if requests is None:
        return _empty_dict()

    if tf not in _TIMEFRAME_SECONDS:
        raise ValueError(f"Unsupported timeframe: {tf}")

    base = (getattr(C, "DELTA_BASE_URL", "https://api.india.delta.exchange") or "").rstrip("/")
    url = f"{base}/v2/history/candles"

    sec = _TIMEFRAME_SECONDS[tf]
    limit = max(1, min(int(limit), _MAX_CANDLES))

    sym = symbol or getattr(C, "PAIR", "SOLUSD")

    now_s = int(time.time())
    start_s = now_s - limit * sec
    end_s = max(now_s, start_s + sec)

    params = {"resolution": tf, "symbol": sym, "start": start_s, "end": end_s}

    last_exc = None
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=10)
            # Retry lightly on 5xx
            if r.status_code >= 500:
                time.sleep(0.5)
                continue
            r.raise_for_status()
            js = r.json()
            rows = js.get("result") or js.get("candles") or js.get("data") or js
            return _normalize_ohlcv_rows_to_dict(rows)
        except Exception as e:
            last_exc = e
            time.sleep(0.5)
    # On failure, return empty but well-formed
    return _empty_dict()

# ---------------------------
# Public API
# ---------------------------
def fetch_ohlcv(ex: ccxt.Exchange, tf: str, limit: Optional[int] = None) -> Dict[str, List[float]]:
    """
    Robust OHLCV fetch that tolerates regional payloads and empty responses.
    Strategy:
      1) Try CCXT: ex.fetch_ohlcv(C.PAIR, timeframe=tf, limit=limit)
      2) If empty/error AND exchange is Delta (by config or handle), fallback to REST using C.DELTA_BASE_URL
    Always returns dict of arrays with timestamps in **milliseconds** (may be empty).
    """
    # Choose a robust limit if not provided or invalid
    if not isinstance(limit, int) or limit <= 0:
        limit = _min_bars_for(tf)

    # 1) CCXT path
    try:
        raw = ex.fetch_ohlcv(C.PAIR, timeframe=tf, limit=limit)
        rows = _normalize_ohlcv_rows_to_dict(raw)
        # If CCXT returned anything, use it
        if rows["timestamp"]:
            return rows
    except Exception as e:
        print(f"[OHLCV FETCH ERROR via CCXT] {e}")

    # 2) REST fallback for Delta if configured/likely
    is_delta_cfg = str(getattr(C, "EXCHANGE_ID", "delta")).lower().startswith("delta")
    is_delta_ex  = getattr(ex, "id", "delta").lower().startswith("delta")
    if is_delta_cfg or is_delta_ex:
        try:
            return _delta_rest_fetch(tf, limit, symbol=getattr(C, "PAIR", "SOLUSD"))
        except Exception as e:
            print(f"[OHLCV FETCH ERROR via Delta REST] {e}")

    # Final fallback: empty
    return _empty_dict()

def fetch_balance_quote(ex, pair: str) -> float:
    try:
        quote = quote_from_pair(pair)
        bal = ex.fetch_balance()
        if bal and "free" in bal and quote in bal["free"]:
            return float(bal["free"][quote])
        return 0.0
    except Exception as e:
        raise RuntimeError(f"Delta balance fetch failed: {e}")

def quote_from_pair(pair: str) -> str:
    if "/" in pair:
        return pair.split("/")[1].upper()
    pair = pair.upper()
    for suf in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if pair.endswith(suf):
            return suf
    if pair.endswith("PERP"):
        return "USD"
    return pair[-3:]

# ---- ADD BACK pseudo_delta ----
def pseudo_delta(tf5: Dict[str, List[float]], look: int = 30) -> float:
    """
    Approximate delta from 5m candles:
    positive if closes > opens with volume, negative otherwise.
    """
    closes, opens, vols = tf5.get("close", []), tf5.get("open", []), tf5.get("volume", [])
    n = min(look, len(closes))
    val = 0.0
    for i in range(-n, 0):
        if i >= -len(closes):
            sign = 1 if closes[i] >= opens[i] else -1
            vol  = vols[i] if i >= -len(vols) else 0.0
            val += sign * vol
    return val