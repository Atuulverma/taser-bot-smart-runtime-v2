import time
from typing import Dict, List

from . import config as C
from .ai_rm import decide as ai_decide
from .messenger import tg_send
from .money import calc_pnl


class ScalpBrain:
    def __init__(self, pair: str, side: str, entry: float, qty: float):
        self.pair = pair
        self.side = side
        self.entry = entry
        self.qty = qty

        self.mfe = 0.0
        self.mae = 0.0
        self.start_ts = time.time()
        self.last_action = "INIT"

    def update_extrema(self, last_price: float):
        pnl = calc_pnl(self.side, self.entry, last_price, self.qty)
        self.mfe = max(self.mfe, pnl)
        self.mae = min(self.mae, pnl)

    async def step(self, ex, price_now: float, tf1m: Dict, tf5: Dict, tf15: Dict, draft):
        self.update_extrema(price_now)
        age = time.time() - self.start_ts

        features = {
            "pair": self.pair,
            "side": self.side,
            "entry": self.entry,
            "price_now": price_now,
            "qty": self.qty,
            "mfe": self.mfe,
            "mae": self.mae,
            "age_sec": age,
            "tp3": (draft.tps[-1] if draft.tps else None),
            "rsi_1m": _safe_rsi(tf1m),
            "rsi_5m": _safe_rsi(tf5),
            "mom_1m": _slope(tf1m["close"], 8),
            "mom_5m": _slope(tf5["close"], 8),
            "wick_intensity": _wick_intensity(tf1m),
            "vol_1m": (sum(tf1m["volume"][-10:]) / 10 if len(tf1m["volume"]) >= 10 else None),
            "vol_5m": (sum(tf5["volume"][-10:]) / 10 if len(tf5["volume"]) >= 10 else None),
        }

        decision = ai_decide(features)
        action = decision.get("action", "HOLD")
        why = decision.get("why", "")
        conf = decision.get("confidence", 0.0)

        self.last_action = action
        if action == "MOVE_SL_BE":
            await tg_send(f"[SCALP] Move SL -> BE | {why} (conf {conf:.2f})")
        elif action == "TRAIL_TIGHT":
            await tg_send(f"[SCALP] Tighten trailing stop | {why} (conf {conf:.2f})")
        elif action == "TRIM":
            await tg_send(f"[SCALP] Trim partial | {why} (conf {conf:.2f})")
        elif action == "EXTEND":
            await tg_send(f"[SCALP] Extend target | {why} (conf {conf:.2f})")
        elif action == "EXIT":
            await tg_send(f"[SCALP] EXIT now | {why} (conf {conf:.2f})")
            return "EXIT"

        if C.SCALP_ENABLED and age > C.SCALP_MAX_HOLD_SECONDS and self.mfe <= 0:
            await tg_send("[SCALP] Time stop hit; suggest EXIT/BE")
            return "EXIT"
        return action


def _safe_rsi(tf):
    from .indicators import rsi

    rs = rsi(tf["close"], 14)
    return rs[-1] if rs else None


def _slope(series: List[float], n: int) -> float:
    if len(series) < n + 1:
        return 0.0
    window = series[-n - 1 :]
    return (window[-1] - window[0]) / max(abs(window[0]), 1e-9)


def _wick_intensity(tf1m: Dict) -> float:
    if len(tf1m["high"]) < 20:
        return 0.0

    highs = tf1m["high"][-20:]
    lows = tf1m["low"][-20:]
    closes = tf1m["close"][-20:]
    opens = tf1m["open"][-20:]

    w = 0.0
    for hi, lo, cl, opn in zip(highs, lows, closes, opens):
        body = abs(cl - opn)
        rng = hi - lo
        if rng > 0:
            w += max(rng - body, 0) / rng
    return w / 20.0
