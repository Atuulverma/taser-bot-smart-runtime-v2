import os

import ccxt
from dotenv import load_dotenv

load_dotenv()

# Read keys and India URL from .env
API_KEY = os.getenv("DELTA_API_KEY", "")
API_SECRET = os.getenv("DELTA_API_SECRET", "")
BASE_URL = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")
PAIR = os.getenv("PAIR", "SOL/USDT")

# Build CCXT client
ex = ccxt.delta(
    {
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "enableRateLimit": True,
    }
)
ex.urls["api"] = {
    "public": BASE_URL,
    "private": BASE_URL,
}

print("Using API base:", ex.urls["api"])


# --- Hardened normalizer for testing ---
def normalize_ohlcv(rows):
    if isinstance(rows, list) and (not rows or isinstance(rows[0], (list, tuple))):
        return rows
    if isinstance(rows, dict):
        for key in ("result", "data", "candles", "items"):
            inner = rows.get(key)
            if isinstance(inner, list):
                if inner and isinstance(inner[0], dict):
                    out = []
                    for it in inner:
                        ts = it.get("time") or it.get("timestamp") or it.get("ts")
                        o = it.get("open") or it.get("o")
                        h = it.get("high") or it.get("h")
                        low_val = it.get("low") or it.get("l")
                        c = it.get("close") or it.get("c")
                        v = it.get("volume") or it.get("v")
                        out.append(
                            [
                                int(ts),
                                float(o),
                                float(h),
                                float(low_val),
                                float(c),
                                float(v),
                            ]
                        )
                    return out
                return inner
    raise ValueError(f"Unexpected OHLCV shape: {type(rows)} {str(rows)[:200]}")


# Try fetch
try:
    raw = ex.fetch_ohlcv(PAIR, timeframe="5m", limit=3)
    print("Raw OHLCV type:", type(raw), "len:", (len(raw) if hasattr(raw, "__len__") else "?"))
    rows = normalize_ohlcv(raw)
    print("Normalized:", rows[:2])
except Exception as e:
    print("ERROR fetch_ohlcv:", e)
    if isinstance(e, ccxt.BaseError):
        print("CCXT details:", getattr(e, "params", None))
