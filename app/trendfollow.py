# app/trendfollow.py
# Drop-in "TrendFollow" engine: no-gate trendline/EMA follower.
# - Entry when price breaks regression trendline (highs for LONG, lows for SHORT)
#   or when EMA fast/slow trend confirms direction
# - SL built against opposite trendline with ATR/fee pad; clamped by MIN/MAX rails
# - TPs by R multiples (configurable) and strictly ordered
# - Returns a Signal-compatible object consumed by scheduler.place_bracket
#
# This module is intentionally self-contained and conservative. It does NOT place
# orders itself. It only constructs a Signal. Surveillance should manage exits;
# for TrendFollow, prefer a trendline-reversal exit logic.

from __future__ import annotations
from typing import Dict, List, Optional, Any

from app import config as C

try:
    from app import telemetry as _tele
except Exception:
    _tele = None

def _tlog(ev: str, msg: str, extra: Optional[Dict[str, Any]] = None):
    try:
        if _tele and hasattr(_tele, 'log'):
            _tele.log('trendfollow', ev, msg, extra or {})
    except Exception:
        pass

# -------------------------------------------------
# Public Signal shape (kept minimal and scheduler-compatible)
# -------------------------------------------------
class Signal:
    def __init__(self, side: str, entry: float, sl: float, tps: List[float], reason: str, meta: Dict[str, Any]):
        self.side = side
        self.entry = entry
        self.sl = sl
        self.tps = tps
        self.reason = reason
        self.meta = meta or {}

# -----------------------------
# Small numerics (no 3rd parties)
# -----------------------------

def _ema(vals: List[float], n: int) -> List[float]:
    if n <= 1 or not vals:
        return list(vals)
    k = 2.0 / (n + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out

def _atr(highs: List[float], lows: List[float], n: int = 14) -> List[float]:
    n = max(1, min(n, len(highs), len(lows)))
    tr = [float(highs[i]) - float(lows[i]) for i in range(len(highs))]
    out = []
    for i in range(len(tr)):
        w = min(n, i + 1)
        out.append(sum(tr[i - w + 1 : i + 1]) / w)
    return out

# Simple least-squares regression value at last index for a series
# Returns (slope, intercept, y_at_last, series_index_start)
def _linreg_y(series: List[float], length: int) -> Optional[tuple]:
    if not series or length < 2 or len(series) < length:
        return None
    y = [float(x) for x in series[-length:]]
    # x as 0..L-1
    L = len(y)
    sx = (L - 1) * L / 2.0
    sxx = (L - 1) * L * (2 * L - 1) / 6.0
    sy = sum(y)
    sxy = sum(i * y[i] for i in range(L))
    denom = (L * sxx - sx * sx)
    if abs(denom) < 1e-12:
        return None
    m = (L * sxy - sx * sy) / denom
    b = (sy - m * sx) / L
    y_last = m * (L - 1) + b
    return (m, b, y_last, len(series) - L)

# Build upper/lower regression-based trendlines from highs/lows
# Returns (upper_val_now, lower_val_now, meta)
def _trendlines(highs: List[float], lows: List[float], length: int) -> Optional[tuple]:
    if len(highs) < length or len(lows) < length:
        return None
    up = _linreg_y(highs, length)
    dn = _linreg_y(lows, length)
    if up is None or dn is None:
        return None
    m_hi, b_hi, y_hi, _ = up
    m_lo, b_lo, y_lo, _ = dn
    meta = {
        "linreg": {
            "m_high": m_hi,
            "b_high": b_hi,
            "m_low": m_lo,
            "b_low": b_lo,
            "len": length,
        }
    }
    return (float(y_hi), float(y_lo), meta)

# --------------------------------------
# Public entry function used by scheduler
# --------------------------------------

def follow_signal(price: float,
                  tf5: Dict[str, List[float]],
                  tf15: Dict[str, List[float]],
                  tf1h: Dict[str, List[float]],
                  pdh: Optional[float],
                  pdl: Optional[float],
                  tf1m: Optional[Dict[str, List[float]]] = None) -> Signal:
    """
    No-gates TrendFollow:
    - Direction: break of regression TL (5m) OR EMA fast>slow / fast<slow
    - SL: opposite TL ± pad (ATR + fee cushion), clamped to entry-based rails
    - TP: R-multiples from config (TF_TP_R or TS_TP_R or TP_R_MULTIS)
    - Meta: includes current TLs & EMAs; engine is set to 'trendfollow'
    - Two-stage absolute profit lock during manage(): +$0.25 then +TF_ABS_LOCK_USD (e.g., $0.50), fee-adjusted; ratchets only, never auto-closes
    """
    # Defaults & guards
    closes = (tf5 or {}).get("close") or []
    highs  = (tf5 or {}).get("high") or []
    lows   = (tf5 or {}).get("low") or []

    # Parameters
    tl_len   = int(getattr(C, "TF_TL_LOOKBACK", getattr(C, "TS_TL_LOOKBACK", 14)))
    ema_fast_n = int(getattr(C, "TF_EMA_FAST", 8))
    ema_slow_n = int(getattr(C, "TF_EMA_SLOW", 20))

    # Require only as many bars as needed by the model, plus a small buffer
    need_bars = max(tl_len, ema_slow_n, 20) + 10   # typically 30–40 bars, not 60

    if len(closes) < need_bars or len(highs) < need_bars or len(lows) < need_bars:
        _tlog('NONE', 'insufficient_data', {
            'need_bars': int(need_bars),
            'len_close': len(closes), 'len_high': len(highs), 'len_low': len(lows)
        })
        return Signal("NONE", 0.0, 0.0, [], "trendfollow: insufficient data", {"engine": "trendfollow"})

    # Compute trendlines & EMAs (5m)
    tl = _trendlines(highs, lows, tl_len)
    if tl is None:
        _tlog('NONE', 'trendline_calc_failed', {'tl_len': tl_len})
        return Signal("NONE", 0.0, 0.0, [], "trendfollow: trendline calc failed", {"engine": "trendfollow"})
    tl_upper, tl_lower, tl_meta = tl

    ema_fast = _ema(closes, ema_fast_n)
    ema_slow = _ema(closes, ema_slow_n)
    ema_up = ema_fast[-1] > ema_slow[-1]
    ema_dn = ema_fast[-1] < ema_slow[-1]

    # Triggers (no additional gates)
    upper_break = price > tl_upper
    lower_break = price < tl_lower

    side: str = "NONE"
    if upper_break or ema_up:
        side = "LONG"
    elif lower_break or ema_dn:
        side = "SHORT"
    else:
        _tlog('NONE', 'no_break_or_ema', {
            'price': float(price), 'tl_upper': float(tl_upper), 'tl_lower': float(tl_lower),
            'ema_up': bool(ema_up), 'ema_dn': bool(ema_dn)
        })
        return Signal("NONE", 0.0, 0.0, [], "trendfollow: no break/EMA trend", {"engine": "trendfollow"})

    # Risk model
    atr14 = _atr(highs, lows, 14)[-1]
    fee_pct = float(getattr(C, "FEE_PCT", 0.0005))
    fee_pad_mult = float(getattr(C, "FEE_PAD_MULT", 2.0))
    fee_pad = price * fee_pct * fee_pad_mult
    pad = max(0.6 * atr14, fee_pad)

    min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
    max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))

    if side == "LONG":
        raw_sl = min(price - pad, tl_lower - pad)
        lo = price - price * max_pct
        hi = price - price * min_pct
        sl = max(min(raw_sl, hi), lo)
    else:
        raw_sl = max(price + pad, tl_upper + pad)
        lo2 = price + price * min_pct
        hi2 = price + price * max_pct
        sl = min(max(raw_sl, lo2), hi2)

    sl = float(round(sl, 4))
    entry = float(round(price, 4))

    # TPs: prefer TF_TP_R, else TS_TP_R, else TP_R_MULTIS
    tf_tp_r = getattr(C, "TF_TP_R", None)
    if tf_tp_r is None:
        tf_tp_r = getattr(C, "TS_TP_R", None)
    if tf_tp_r is None:
        tp_mults = list(getattr(C, "TP_R_MULTIS", [0.8, 1.4, 2.2]))
    else:
        try:
            # Accept formats like "0.8,1.4,2.2" or "[0.8, 1.4, 2.2]" or "(0.8,1.4,2.2)"
            val = str(tf_tp_r).strip()
            # strip common container chars
            if (val.startswith("[") and val.endswith("]")) or (val.startswith("(") and val.endswith(")")) or (val.startswith("{") and val.endswith("}")):
                val = val[1:-1]
            parts = [p for p in val.replace(" ", "").split(",") if p]
            tp_mults = [float(p) for p in parts]
            if not tp_mults:
                raise ValueError("empty tp list")
        except Exception:
            tp_mults = [0.8, 1.4, 2.2]

    R = max(1e-9, abs(entry - sl))
    raw_tps = [(entry + m * R) if side == "LONG" else (entry - m * R) for m in tp_mults[:3]]

    # Ensure we have at least one TP even in edge cases
    if not raw_tps:
        # fallback to a single 0.8R target in the correct direction
        m = 0.8
        raw_tps = [(entry + m * R)] if side == "LONG" else [(entry - m * R)]

    # Order and dedupe TPs strictly in the profit direction
    if side == "LONG":
        tps = sorted({round(x, 4) for x in raw_tps if x > entry}, key=lambda z: z)[:3]
    else:
        tps = sorted({round(x, 4) for x in raw_tps if x < entry}, key=lambda z: z, reverse=True)[:3]

    meta = {
        "engine": "trendfollow",
        "ema": {"fast": float(ema_fast[-1]), "slow": float(ema_slow[-1])},
        "tl": {"upper": float(tl_upper), "lower": float(tl_lower), "len": tl_len},
        "risk": {"atr14": float(atr14), "pad": float(pad)},
    }

    why = []
    if upper_break: why.append("upper-break")
    if lower_break: why.append("lower-break")
    if ema_up:      why.append("ema-up")
    if ema_dn:      why.append("ema-dn")
    reason = "TrendFollow: " + ", ".join(why) if why else "TrendFollow"

    _tlog('OK', 'signal', {
        'side': side, 'entry': entry, 'sl': sl,
        'tp1': tps[0] if tps else None,
        'tl_u': meta['tl']['upper'], 'tl_l': meta['tl']['lower'],
        'ema_f': meta['ema']['fast'], 'ema_s': meta['ema']['slow']
    })

    return Signal(side, entry, sl, tps, reason, meta)

# --------------------------------------
# Optional manager hook for surveillance
# --------------------------------------

def _compute_tps_for_manage(entry: float, sl: float, side: str) -> List[float]:
    """
    Recompute TP levels by R-multiples using the same rules as follow_signal(),
    but kept local for manage() to avoid imports/side effects. R = |entry - sl|.
    """
    # TPs: prefer TF_TP_R, else TS_TP_R, else TP_R_MULTIS
    tf_tp_r = getattr(C, "TF_TP_R", None)
    if tf_tp_r is None:
        tf_tp_r = getattr(C, "TS_TP_R", None)
    if tf_tp_r is None:
        tp_mults = list(getattr(C, "TP_R_MULTIS", [0.8, 1.4, 2.2]))
    else:
        try:
            val = str(tf_tp_r).strip()
            if (val.startswith("[") and val.endswith("]")) or (val.startswith("(") and val.endswith(")")) or (val.startswith("{") and val.endswith("}")):
                val = val[1:-1]
            parts = [p for p in val.replace(" ", "").split(",") if p]
            tp_mults = [float(p) for p in parts]
            if not tp_mults:
                raise ValueError("empty tp list")
        except Exception:
            tp_mults = [0.8, 1.4, 2.2]

    R = max(1e-9, abs(float(entry) - float(sl)))
    raw_tps = [(entry + m * R) if str(side).upper() == "LONG" else (entry - m * R) for m in tp_mults[:3]]

    if str(side).upper() == "LONG":
        tps = sorted({round(float(x), 4) for x in raw_tps if float(x) > float(entry)}, key=lambda z: z)[:3]
    else:
        tps = sorted({round(float(x), 4) for x in raw_tps if float(x) < float(entry)}, key=lambda z: z, reverse=True)[:3]
    return tps

def manage(price: float,
           side: str,
           entry: float,
           sl: float,
           tps: List[float],
           tf5: Dict[str, List[float]],
           meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    TrendFollow manage shim:
    - Recompute 5m regression trendlines and EMAs
    - If reversal vs current position, request reverse exit (close on line cross)
    - Returns a dict surveillance understands:
      {
        'reverse': True/False,
        'reverse_on': 'close_cross_down'|'close_cross_up'|'ema_flip',
        'line': <float>,
        'line_px': <float>,
        'why': 'text'
      }
    """
    closes = (tf5 or {}).get("close") or []
    highs  = (tf5 or {}).get("high") or []
    lows   = (tf5 or {}).get("low") or []
    out: Dict[str, Any] = {"reverse": False}

    # Config knobs (read from app.config)
    tl_len   = int(getattr(C, "TF_TL_LOOKBACK", getattr(C, "TS_TL_LOOKBACK", 14)))
    use_close = bool(getattr(C, "TF_EXIT_USE_CLOSE", True))
    confirm_n = int(getattr(C, "TF_EXIT_CONFIRM_BARS", 0))
    buf_atr_mult = float(getattr(C, "TF_EXIT_BUFFER_ATR", 0.15))
    buf_pct      = float(getattr(C, "TF_EXIT_BUFFER_PCT", 0.0005))
    use_ema_flip = bool(getattr(C, "TF_USE_EMA_FLIP", False))

    # Require only as many bars as needed by the model, plus a small buffer
    ema_slow_n = int(getattr(C, "TF_EMA_SLOW", 20))
    need_bars = max(tl_len, ema_slow_n, 20) + 10
    if len(closes) < need_bars or len(highs) < need_bars or len(lows) < need_bars:
        return out

    tl = _trendlines(highs, lows, tl_len)
    if tl is None:
        return out
    tl_upper, tl_lower, tl_meta = tl

    # Line slope for historical checks
    def _line_shift(y_last: float, slope: float, back: int) -> float:
        return float(y_last - slope * back)
    m_hi = float(tl_meta.get("linreg", {}).get("m_high", 0.0))
    m_lo = float(tl_meta.get("linreg", {}).get("m_low", 0.0))

    # Use the last CLOSED 5m bar
    c_last = float(closes[-1])

    atr14 = _atr(highs, lows, 14)[-1]
    buf = max(atr14 * buf_atr_mult, c_last * buf_pct)

    # Optional EMA filter
    ema_fast_n = int(getattr(C, "TF_EMA_FAST", 8))
    ema_slow_n = int(getattr(C, "TF_EMA_SLOW", 20))
    ema_fast = _ema(closes, ema_fast_n)
    ema_slow = _ema(closes, ema_slow_n)
    ema_up = ema_fast[-1] > ema_slow[-1]
    ema_dn = ema_fast[-1] < ema_slow[-1]

    def crossed(up: bool) -> bool:
        # up=True → check close above upper; up=False → below lower
        for k in range(0, max(1, confirm_n + 1)):
            ck = float(closes[-1 - k])
            if up:
                line_k = _line_shift(tl_upper, m_hi, k)
                if ck <= line_k + buf:
                    return False
            else:
                line_k = _line_shift(tl_lower, m_lo, k)
                if ck >= line_k - buf:
                    return False
        return True

    trigger = None
    line_used = None

    if str(side).upper() == "LONG":
        if (use_close and crossed(False)) or (not use_close and (c_last < tl_lower - buf)):
            trigger = "close_cross_down"; line_used = tl_lower
        elif use_ema_flip and ema_dn:
            trigger = "ema_flip"; line_used = tl_lower
    elif str(side).upper() == "SHORT":
        if (use_close and crossed(True)) or (not use_close and (c_last > tl_upper + buf)):
            trigger = "close_cross_up"; line_used = tl_upper
        elif use_ema_flip and ema_up:
            trigger = "ema_flip"; line_used = tl_upper

    if trigger:
        out.update({
            "reverse": True,
            "reverse_on": trigger,
            "line": float(line_used) if line_used is not None else None,
            "line_px": float(line_used) if line_used is not None else None,
            "why": f"trendfollow {trigger} buf={buf:.4f} confirm={confirm_n}",
            "engine": "trendfollow"
        })
    else:
        out.update({
            "reverse": False,
            "reverse_on": None,
            "line": float(tl_lower if str(side).upper() == "LONG" else tl_upper),
            "line_px": float(tl_lower if str(side).upper() == "LONG" else tl_upper),
            "engine": "trendfollow"
        })

    # --- TASER-style absolute profit lock (two-stage) and spam suppression ---

    # Config/thresholds
    # Stage 1 is intentionally internal (0.25 USD). Stage 2 is configurable (TF_ABS_LOCK_USD, e.g., 0.50)
    first_lock_usd = float(getattr(C, "TF_FIRST_LOCK_USD", 0.25))  # do not expose by default; falls back to 0.25
    abs_lock_usd   = float(getattr(C, "TF_ABS_LOCK_USD", 0.0))     # set to 0.50 in .env/config to enable stage 2
    min_sl_change_abs = float(getattr(C, "TF_MIN_SL_CHANGE_ABS", 0.01))

    # Fee pad for realistic lock (covers fees so realized PnL >= target)
    fee_pct = float(getattr(C, "FEE_PCT", 0.0005))
    fee_pad_mult = float(getattr(C, "FEE_PAD_MULT", 2.0))
    fee_pad = price * fee_pct * fee_pad_mult

    # Helper to ratchet SL only towards profit and avoid tiny/no-op updates
    def _ratchet_sl(cur_sl: float, target_sl: float) -> Optional[float]:
        try:
            new_sl = float(round(target_sl, 6))
            if abs(new_sl - float(cur_sl)) >= float(min_sl_change_abs):
                return new_sl
        except Exception:
            pass
        return None

    lock_stage = 0
    lock_amt = 0.0

    if side.upper() in ("LONG", "SHORT"):
        # unrealized profit in USD terms relative to entry (approx)
        profit = (price - entry) if side.upper() == "LONG" else (entry - price)

        # Decide which stage is qualified
        if profit >= max(abs_lock_usd, 0.0) and abs_lock_usd > 0.0:
            lock_stage = 2
            lock_amt = abs_lock_usd
        elif profit >= first_lock_usd:
            lock_stage = 1
            lock_amt = first_lock_usd

        if lock_stage > 0:
            if side.upper() == "LONG":
                target_sl = entry + lock_amt + fee_pad
                if target_sl > sl:
                    new_sl = _ratchet_sl(sl, target_sl)
                    if new_sl is not None:
                        out['sl'] = new_sl
                        why0 = out.get('why', '')
                        out['why'] = (why0 + f" lock{lock_stage}@${lock_amt:.2f}").strip()
            else:  # SHORT
                target_sl = entry - lock_amt - fee_pad
                if target_sl < sl:
                    new_sl = _ratchet_sl(sl, target_sl)
                    if new_sl is not None:
                        out['sl'] = new_sl
                        why0 = out.get('why', '')
                        out['why'] = (why0 + f" lock{lock_stage}@${lock_amt:.2f}").strip()

            # annotate
            out['lock_stage'] = lock_stage
            out['lock_amt'] = round(lock_amt, 4)

    # --- TP de-jitter & spam suppression (mirror of TrendScalp fix) ---
    try:
        # Use TF_MIN_TP_CHANGE_ABS if present; else fall back to TF_MIN_SL_CHANGE_ABS; else TS_MIN_SL_CHANGE_ABS; else 0.01
        tp_eps = float(getattr(C, "TF_MIN_TP_CHANGE_ABS",
                         getattr(C, "TF_MIN_SL_CHANGE_ABS",
                         getattr(C, "TS_MIN_SL_CHANGE_ABS", 0.01))))

        # Only recompute if we have a valid side and the current SL (possibly ratcheted) is known
        cur_sl = float(out.get('sl', sl)) if isinstance(out.get('sl', None), (int, float)) else float(sl)
        proposed_tps = _compute_tps_for_manage(entry=float(entry), sl=cur_sl, side=str(side))
        # Compare to existing tps list passed in
        same_len = len(proposed_tps) == len(tps)
        materially_changed = False
        if same_len:
            for a, b in zip(proposed_tps, tps):
                if abs(float(a) - float(b)) >= tp_eps:
                    materially_changed = True
                    break
        else:
            # length mismatch → consider it a material change
            materially_changed = True

        if materially_changed:
            out['tps'] = proposed_tps  # surveillance will decide whether to replace orders
            why0 = out.get('why', '')
            out['why'] = (why0 + " tp_refresh").strip()
    except Exception:
        # Never let TP re-eval break manage(); just skip on error
        pass

    return out
