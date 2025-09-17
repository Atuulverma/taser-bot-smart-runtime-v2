from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional

try:
    import pandas as pd
except Exception:
    pd = None

try:
    import app.config as C
except Exception:
    import importlib as _importlib

    C = _importlib.import_module("config")

from app.trendscalp import scalp_manage, scalp_signal


def _load_parquet_series(symbol: str, tf: str, root: str, since: dt.date, until: dt.date):
    base = Path(root) / symbol / tf
    frames = []
    for f in sorted(base.glob("*.parquet")):
        try:
            day = dt.datetime.strptime(f.stem.split("_")[-1], "%Y-%m-%d").date()
        except Exception:
            continue
        if day < since or day > until:
            continue
        frames.append(pd.read_parquet(f))
    if not frames:
        raise RuntimeError(f"No parquet for {symbol} {tf} between {since} and {until}")
    df = pd.concat(frames).sort_values("ts_ms")
    return df


def _series_to_tf(df: "pd.DataFrame") -> Dict[str, List[float]]:
    return {
        "timestamp": list(map(int, df["ts_ms"].tolist())),
        "open": df["o"].astype(float).tolist(),
        "high": df["h"].astype(float).tolist(),
        "low": df["l"].astype(float).tolist(),
        "close": df["c"].astype(float).tolist(),
        "volume": df["v"].astype(float).tolist(),
    }


def run(symbol: str, start: str, end: str, tf5: str = "5m") -> None:
    if pd is None:
        print("[sim] pandas not installed")
        return
    since = dt.datetime.strptime(start, "%Y-%m-%d").date()
    until = dt.datetime.strptime(end, "%Y-%m-%d").date()
    root = getattr(C, "DATASET_ROOT", "datasets")

    df5 = _load_parquet_series(symbol, tf5, root, since, until)
    # Build naive 15m/1h by resampling (placeholder)
    df = df5.copy()
    df["ts"] = pd.to_datetime(df["ts_ms"], unit="ms")
    df15 = (
        df.set_index("ts")
        .resample("15min")
        .agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
        .dropna()
        .reset_index()
    )
    df15["ts_ms"] = (df15["ts"].astype("int64") // 10**6).astype(int)
    df1h = (
        df.set_index("ts")
        .resample("1h")
        .agg({"o": "first", "h": "max", "l": "min", "c": "last", "v": "sum"})
        .dropna()
        .reset_index()
    )
    df1h["ts_ms"] = (df1h["ts"].astype("int64") // 10**6).astype(int)

    tf5_s = _series_to_tf(df5)
    tf15_s = _series_to_tf(df15.rename(columns={"o": "o", "h": "h", "l": "l", "c": "c", "v": "v"}))
    tf1h_s = _series_to_tf(df1h.rename(columns={"o": "o", "h": "h", "l": "l", "c": "c", "v": "v"}))

    equity = float(getattr(C, "SIMULATOR_START_CAPITAL", 10000))
    fee_pct = float(getattr(C, "SIMULATOR_FEE_PCT", 0.0005))
    open_side: Optional[str] = None
    entry = sl = 0.0
    tps: List[float] = []
    peak_equity = equity
    max_dd = 0.0
    wins = losses = 0

    for i in range(250, len(tf5_s["close"])):
        tf5_now = {k: v[: i + 1] for k, v in tf5_s.items()}
        tf15_now = {k: v[: max(1, min(len(v), i // 3))] for k, v in tf15_s.items()}
        tf1h_now = {k: v[: max(1, min(len(v), i // 12))] for k, v in tf1h_s.items()}

        price = tf5_now["close"][-1]

        if open_side is None:
            sig = scalp_signal(price, tf5_now, tf15_now, tf1h_now, None, None, None, None)
            if sig.side in ("LONG", "SHORT"):
                # simplistic risk sizing 1% risk, qty calc from SL distance
                sl_dist = abs(price - sig.sl) / max(1e-9, price)
                risk_cap = equity * 0.01
                qty = risk_cap / max(1e-9, sl_dist * price)
                # apply fees at open
                equity -= price * qty * fee_pct
                open_side, entry, sl, tps = sig.side, sig.entry, sig.sl, sig.tps
        else:
            # manage
            mg = scalp_manage(price, open_side, entry, sl, tps, tf5_now, {"ml_conf": 0.6})
            sl = mg.get("sl", sl)
            exit_now = bool(mg.get("exit", False))
            if exit_now:
                # close at price, apply fees
                qty = equity * 0.0 + 1.0  # placeholder qty
                # compute PnL in R terms roughly; qty unknown in this stub
                pnl = (price - entry) if open_side == "LONG" else (entry - price)
                pnl_after_fee = pnl - (price + entry) * fee_pct * 0.5
                equity += pnl_after_fee
                if pnl_after_fee >= 0:
                    wins += 1
                else:
                    losses += 1
                open_side = None
                entry = sl = 0.0
                tps = []

        peak_equity = max(peak_equity, equity)
        max_dd = max(max_dd, (peak_equity - equity))

    print(f"[sim] equity={equity:.2f} maxDD={max_dd:.2f} W/L={wins}/{losses}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--from", dest="since", required=True)
    ap.add_argument("--to", dest="until", required=True)
    ap.add_argument("--tf", default="5m")
    args = ap.parse_args()
    run(args.symbol, args.since, args.until, args.tf)


if __name__ == "__main__":
    main()
