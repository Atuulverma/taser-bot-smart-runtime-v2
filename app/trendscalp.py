"""
TrendScalp engine — portable clones of two Pine ideas:
1) Lorentzian Classification (jdehorty)
2) Trendlines With Breaks (LuxAlgo)

Avoid-zones are OFF by default (toggle via TRENDSCALP_USE_AVOID_ZONES).
Returns a taser_rules.Signal so the existing pipeline stays intact.
Two-stage absolute profit locks exist but are **paused by
default for TrendScalp**
(see TRENDSCALP_PAUSE_ABS_LOCKS).
"""

import math  # noqa: I001
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Callable, Tuple, cast


if TYPE_CHECKING:
    # During type-checking, always use the package import to prevent duplicate module mapping.
    import app.config as C
else:
    try:
        import app.config as C
    except Exception:  # pragma: no cover
        import importlib as _importlib

        # Runtime fallback only; mypy won't analyze this branch.
        C = _importlib.import_module("config")

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.taser_rules import Signal
else:
    try:
        from app.taser_rules import Signal
    except Exception:  # pragma: no cover
        from dataclasses import dataclass
        from typing import Dict, List

        @dataclass
        class Signal:  # type: ignore[no-redef]
            side: str
            entry: float
            sl: float
            tps: List[float]
            reason: str
            meta: Dict


# NOTE: _order_tps, _enforce_min_r, and _tp_guard are provided in components/guards.py
# and are not used directly in this module anymore. Removing them here avoids import-time
# failures when taser_rules does not export these symbols.


if TYPE_CHECKING:
    import app.telemetry as telemetry
else:
    try:
        import app.telemetry as telemetry
    except Exception:  # pragma: no cover
        import importlib as _importlib  # noqa: F401

        telemetry = _importlib.import_module("telemetry")

# --- regime classification import ---
from app.regime import classify as classify_regime

# --- helpers: safe config coercion ---


def _env_int(key: str, default: int) -> int:
    """Best-effort int conversion for values coming from config.
    Handles None/float/str and falls back to default on error.
    """
    try:
        v = getattr(C, key, default)
    except Exception:
        return default
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return default


# --- TrendScalp local re-entry memory (per-process, avoids db dependency) ---
_TS_LAST_ENTRY_PX: Optional[float] = None
_TS_LAST_ENTRY_SIDE: Optional[str] = None
_TS_LAST_ENTRY_BAR_TS: Optional[int] = None  # 5m bar timestamp (epoch ms or provider ts)


def _bars_since(ts_list: List, last_ts: Optional[int]) -> Optional[int]:
    """Return number of 5m bars since last_ts using tf5['timestamp'] if available."""
    if last_ts is None or not isinstance(ts_list, list) or not ts_list:
        return None
    try:
        ts_list[-1]
        # Find index of last_ts if present, else estimate by distance (best-effort)
        if last_ts in ts_list:
            i_last = max(0, ts_list.index(last_ts))
            return max(0, len(ts_list) - 1 - i_last)
    except Exception:
        pass
    return None


# ---------- small tech ----------
def _ema(arr: List[float], n: int) -> List[float]:
    if not arr:
        return []
    n = max(1, int(n))
    k = 2.0 / (n + 1.0)
    out = [float(arr[0])]
    for i in range(1, len(arr)):
        out.append(out[-1] + k * (float(arr[i]) - out[-1]))
    return out


def _sma(arr: List[float], n: int) -> List[float]:
    n = max(1, int(n))
    out = []
    s = 0.0
    for i, x in enumerate(arr):
        s += float(x)
        if i >= n:
            s -= float(arr[i - n])
        out.append(s / max(1, min(i + 1, n)))
    return out


def _rsi(closes: List[float], n: int = 14) -> List[float]:
    n = max(1, int(n))
    rsis = []
    up = 0.0
    dn = 0.0
    for i in range(len(closes)):
        if i == 0:
            rsis.append(50.0)
            continue
        ch = closes[i] - closes[i - 1]
        up = (up * (n - 1) + max(0.0, ch)) / n
        dn = (dn * (n - 1) + max(0.0, -ch)) / n
        rs = up / max(1e-12, dn)
        rsis.append(100.0 - 100.0 / (1.0 + rs))
    return rsis


def _cci(closes: List[float], highs: List[float], lows: List[float], n: int = 20) -> List[float]:
    tp = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    sma = _sma(tp, n)
    out = []
    for i in range(len(closes)):
        s = max(1, i + 1)
        k = max(1, min(n, s))
        mean = sma[i]
        dev = sum(abs(tp[j] - mean) for j in range(s - k, s)) / k
        out.append(0.015 * (tp[i] - mean) / max(1e-12, dev))
    return out


def _adx(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> List[float]:
    n = max(1, int(n))
    tr = [0.0]
    plus_dm = [0.0]
    minus_dm = [0.0]
    for i in range(1, len(closes)):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        tr_i = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        tr.append(tr_i)
        plus_dm.append(up if (up > dn and up > 0) else 0.0)
        minus_dm.append(dn if (dn > up and dn > 0) else 0.0)
    tr_ema = _ema(tr, n)
    p_ema = _ema(plus_dm, n)
    m_ema = _ema(minus_dm, n)
    out = []
    for i in range(len(closes)):
        pdi = 100.0 * p_ema[i] / max(1e-12, tr_ema[i])
        mdi = 100.0 * m_ema[i] / max(1e-12, tr_ema[i])
        dx = 100.0 * abs(pdi - mdi) / max(1e-12, (pdi + mdi))
        out.append(dx)
    return _ema(out, n)


def _wavetrend(hlc3: List[float], chlen: int = 10, avg: int = 11) -> List[float]:
    esa = _ema(hlc3, chlen)
    d = [abs(hlc3[i] - esa[i]) for i in range(len(hlc3))]
    de = _ema(d, chlen)
    ci = [(hlc3[i] - esa[i]) / max(1e-12, 0.015 * de[i]) for i in range(len(hlc3))]
    return _ema(ci, avg)


def _atr(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> List[float]:
    trs = [0.0]
    for i in range(1, len(closes)):
        trs.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
    return _ema(trs, n)


def _parse_floats_csv(val, default="0.8,1.4,2.2") -> List[float]:
    """Parse floats from env/config that may be CSV or JSON-like lists.
    Examples accepted: "0.8,1.4,2.2" or "[0.8, 1.4, 2.2]" or [0.8,1.4,2.2].
    """
    if isinstance(val, (list, tuple)):
        out = []
        for x in val:
            try:
                out.append(float(x))
            except Exception:
                pass
        return out or [0.8, 1.4, 2.2]
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = [p.strip() for p in s.split(",")]
    out = []
    for p in parts:
        if not p:
            continue
        try:
            out.append(float(p))
        except Exception:
            # ignore non-numeric fragments
            pass
    if out:
        return out
    # fallback to default
    try:
        ds = str(default).strip()
        if ds.startswith("[") and ds.endswith("]"):
            ds = ds[1:-1]
        return [float(x) for x in ds.split(",") if x]
    except Exception:
        return [0.8, 1.4, 2.2]


# ---------- helper: TP de-jitter comparison ----------
def _tp_diff_exceeds(a: List[float], b: List[float], eps: float) -> bool:
    """Return True if any TP delta exceeds eps (absolute), else False.
    Used to avoid 'TPs replaced' spam when differences are just rounding jitter.
    """
    if not isinstance(a, list) or not isinstance(b, list):
        return True
    if len(a) != len(b):
        return True
    for i in range(len(a)):
        try:
            if abs(float(a[i]) - float(b[i])) > eps:
                return True
        except Exception:
            return True
    return False


# ---------- Lorentzian ANN clone ----------
_DEF_FEATURES = ("RSI", "WT", "CCI", "ADX", "RSI9")


def _feature_series(closes, highs, lows) -> Dict[str, List[float]]:
    hlc3 = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    return {
        "RSI": _rsi(closes, 14),
        "WT": _wavetrend(hlc3, 10, 11),
        "CCI": _cci(closes, highs, lows, 20),
        "ADX": _adx(highs, lows, closes, 20),
        "RSI9": _rsi(closes, 9),
        "HLC3": hlc3,
    }


def _lorentz_distance(vec_now: List[float], vec_hist: List[float]) -> float:
    return sum(math.log(1.0 + abs(vec_now[i] - vec_hist[i])) for i in range(len(vec_now)))


def _ann_predict(
    closes, highs, lows, k: int, max_back: int, feature_count: int
) -> Tuple[int, float]:
    n = len(closes)
    if n < 6:
        return 0, 0.0
    feats = _feature_series(closes, highs, lows)
    keys = list(_DEF_FEATURES)[: max(2, min(5, int(feature_count)))]
    series = [feats[k] for k in keys]
    preds = []
    dists = []
    last_d = -1.0
    back = min(max_back, n - 5)
    start = n - back
    for i in range(start, n - 5):
        if i % 4 != 0:
            continue
        vec = [s[i] for s in series]
        now = [s[-1] for s in series]
        d = _lorentz_distance(now, vec)
        if d >= last_d:
            last_d = d
            dists.append(d)
            label = 1 if closes[i + 4] > closes[i] else (-1 if closes[i + 4] < closes[i] else 0)
            preds.append(label)
            if len(preds) > k:
                q = max(0, int(math.floor(k * 3 / 4)))
                last_d = dists[q] if q < len(dists) else d
                dists = dists[1:]
                preds = preds[1:]
    s = sum(preds)
    return (1 if s > 0 else (-1 if s < 0 else 0), float(s))


# ---------- Trendlines-with-breaks clone ----------
def _trendlines(highs, lows, closes, length: int, method: str, mult: float):
    n = len(closes)
    ph = [None] * n
    pl = [None] * n
    L = max(1, int(length))
    for i in range(L, n - L):
        if all(highs[i] >= highs[i - d] for d in range(1, L + 1)) and all(
            highs[i] > highs[i + d] for d in range(1, L + 1)
        ):
            ph[i] = highs[i]
        if all(lows[i] <= lows[i - d] for d in range(1, L + 1)) and all(
            lows[i] < lows[i + d] for d in range(1, L + 1)
        ):
            pl[i] = lows[i]

    def slope_val(i):
        if method == "stdev":
            m = max(1, L)
            mean = sum(closes[i - m + 1 : i + 1]) / m
            var = sum((closes[j] - mean) ** 2 for j in range(i - m + 1, i + 1)) / m
            sd = math.sqrt(var)
            return sd / max(1, m) * mult
        if method == "linreg":
            m = max(1, L)
            xs = list(range(i - m + 1, i + 1))
            ys = closes[i - m + 1 : i + 1]
            xbar = sum(xs) / m
            ybar = sum(ys) / m
            num = sum((xs[t] - xbar) * (ys[t] - ybar) for t in range(m))
            den = sum((xs[t] - xbar) ** 2 for t in range(m)) or 1.0
            beta = num / den
            return abs(beta) / 2.0 * mult
        atr = _atr(highs, lows, closes, L)
        return atr[i] / max(1, L) * mult

    upper = [0.0] * n
    lower = [0.0] * n
    s_ph = 0.0
    s_pl = 0.0
    for i in range(n):
        val_ph = ph[i]
        if val_ph is not None:
            s_ph = slope_val(i)
            upper[i] = float(val_ph)
        else:
            upper[i] = upper[i - 1] - s_ph if i > 0 else closes[0]
        val_pl = pl[i]
        if val_pl is not None:
            s_pl = slope_val(i)
            lower[i] = float(val_pl)
        else:
            lower[i] = lower[i - 1] + s_pl if i > 0 else closes[0]
    upos = [0] * n
    dnos = [0] * n
    for i in range(n):
        cond_up = (ph[i] is None) and (closes[i] > upper[i] - s_ph * L)
        cond_dn = (pl[i] is None) and (closes[i] < lower[i] + s_pl * L)
        upos[i] = (
            1
            if cond_up
            and (i > 0 and not ((ph[i - 1] is None) and (closes[i - 1] > upper[i - 1] - s_ph * L)))
            else 0
        )
        dnos[i] = (
            1
            if cond_dn
            and (i > 0 and not ((pl[i - 1] is None) and (closes[i - 1] < lower[i - 1] + s_pl * L)))
            else 0
        )
    return upper, lower, upos, dnos


# ---------- public: entry ----------
def scalp_signal(
    price: float,
    tf5: Dict[str, List[float]],
    tf15: Dict[str, List[float]],
    tf1h: Dict[str, List[float]],
    pdh: Optional[float],
    pdl: Optional[float],
    oi_up: Optional[bool],
    delta_pos: Optional[bool],
    tf1m: Optional[Dict[str, List[float]]] = None,
) -> Signal:
    global _TS_LAST_ENTRY_PX, _TS_LAST_ENTRY_SIDE, _TS_LAST_ENTRY_BAR_TS
    if not getattr(C, "TRENDSCALP_ENABLED", False):
        return Signal("NONE", 0, 0, [], "trendscalp disabled", {"engine": "trendscalp"})

    closes = tf5["close"]
    highs = tf5["high"]
    lows = tf5["low"]
    vols = tf5.get("volume", [])
    ts5 = tf5.get("timestamp", [])
    curr_bar_ts = None
    try:
        curr_bar_ts = ts5[-1] if isinstance(ts5, list) and ts5 else None
    except Exception:
        curr_bar_ts = None
    need_bars = max(_env_int("TS_TL_LOOKBACK", 14), _env_int("TS_EMA_SLOW", 20), 20) + 10
    if len(closes) < need_bars or len(highs) < need_bars or len(lows) < need_bars:
        return Signal(
            "NONE",
            0,
            0,
            [],
            "trendscalp: insufficient data",
            {"engine": "trendscalp", "need": int(need_bars), "have": int(len(closes))},
        )

    # --- RE-ENTRY GATES (TrendScalp-only; TASER unaffected) ---
    # A) Same-bar guard (reconfirm next 5m bar before retrying)
    if (
        bool(getattr(C, "REQUIRE_NEW_BAR", True))
        and _TS_LAST_ENTRY_BAR_TS is not None
        and curr_bar_ts is not None
        and _TS_LAST_ENTRY_BAR_TS == curr_bar_ts
    ):
        try:
            telemetry.log(
                "scan",
                "REENTRY_PRE",
                "same 5m bar (REQUIRE_NEW_BAR)",
                {
                    "price": float(price),
                    "side": "NONE",
                    "last_entry": float(_TS_LAST_ENTRY_PX) if _TS_LAST_ENTRY_PX else None,
                    "ago": 0,
                },
            )
        except Exception:
            pass
        return Signal("NONE", 0, 0, [], "trendscalp: same 5m bar", {"engine": "trendscalp"})

    # ML Lorentzian bias (patched: use library gate if enabled)
    _ml_infer: Optional[
        Callable[
            [Dict[str, List[float]], Optional[Dict[str, List[float]]], Optional[str]],
            Tuple[str, float, Optional[str]],
        ]
    ] = None
    try:
        from app.trendscalp_ml_gate import infer_bias_conf as _ml_infer
    except Exception:

        _ml_infer = None
    ml_bias = "neutral"
    ml_sum = 0.0
    ml_conf = 0.0
    ml_regime = None
    if _ml_infer is not None:
        try:
            _bias, _conf, _reg = _ml_infer(tf5, None, None)
            ml_bias = _bias
            ml_conf = float(_conf)
            ml_regime = _reg
        except Exception:
            pass
    if ml_bias == "neutral":
        ml_sign, ml_sum = _ann_predict(
            closes, highs, lows, C.TS_NEIGHBORS, C.TS_MAX_BACK, C.TS_FEATURE_COUNT
        )
        ml_bias = "long" if ml_sign > 0 else ("short" if ml_sign < 0 else "neutral")

        # Trendlines breaks
        upper, lower, upos, dnos = _trendlines(
            highs, lows, closes, C.TS_TL_LOOKBACK, C.TS_TL_SLOPE_METHOD.lower(), C.TS_TL_SLOPE_MULT
        )
        upper_break = bool(upos[-1])
        lower_break = bool(dnos[-1])

        # EMA trend & slope
        ema_fast = _ema(closes, _env_int("TS_EMA_FAST", 8))
        ema_slow = _ema(closes, _env_int("TS_EMA_SLOW", 20))

        def _s(arr, L):
            L = max(2, min(L, len(arr) - 1))
            return (arr[-1] - arr[-L]) / max(1e-9, L)

        ema_up = ema_fast[-1] > ema_slow[-1] and _s(
            ema_fast, _env_int("TS_TREND_SLOPE_LEN", 25)
        ) >= float(getattr(C, "TS_TREND_SLOPE_MIN", 0.0))
        ema_dn = ema_fast[-1] < ema_slow[-1] and _s(
            ema_fast, _env_int("TS_TREND_SLOPE_LEN", 25)
        ) <= -float(getattr(C, "TS_TREND_SLOPE_MIN", 0.0))

        # --- Pine-parity pre-entry filters (no repaint, 5m exec; 15m as higher-TF bias) ---
        # 1) Volatility floor (ATR14(5m)/price)
        atr14_arr = _atr(highs, lows, closes, 14)
        atr14_last = float(atr14_arr[-1])
        vol_floor = float(getattr(C, "TS_VOL_FLOOR_PCT", 0.0020))
        vol_ok = (atr14_last / max(1e-9, price)) >= vol_floor

        # 3) 200-EMA bias on 5m & 15m
        ema200_5 = float(_ema(closes, 200)[-1])
        ema200_15 = None
        if isinstance(tf15, dict) and "close" in tf15 and len(tf15["close"]) >= 200:
            ema200_15 = float(_ema(tf15["close"], 200)[-1])
        # [REVERT_NOTE] Original strict MA gate (keep for quick rollback):
        # ma_long_ok  = (price >= ema200_5) and (ema200_15 is None or price >= ema200_15)
        # ma_short_ok = (price <= ema200_5) and (ema200_15 is None or price <= ema200_15)

        # [PATCH_EMA_RELAX][REMOVE_ON_REVERT] Relaxed MA gate controlled by .env
        _ma_require_15m = bool(getattr(C, "TS_MA_REQUIRE_15M", False))
        _ma_buffer = float(
            getattr(C, "TS_MA_BUFFER_PCT", 0.0015)
        )  # 0.15% buffer around 200-EMA(5m)

        _buf_long = ema200_5 * (1.0 - _ma_buffer)
        _buf_short = ema200_5 * (1.0 + _ma_buffer)

        if _ma_require_15m:
            # strict: require 15m too (still apply small buffer on 5m)
            ma_long_ok = price >= max(
                _buf_long, (ema200_15 if ema200_15 is not None else -float("inf"))
            )
            ma_short_ok = price <= min(
                _buf_short, (ema200_15 if ema200_15 is not None else float("inf"))
            )
        else:
            # relaxed: 5m-only with buffer + trend confirmation from ema_up/ema_dn
            ma_long_ok = (price >= _buf_long) and bool(ema_up)
            ma_short_ok = (price <= _buf_short) and bool(ema_dn)

        # 4) 15-minute RSI side-bias (+ neutral band block)
        rsi15 = None
        if isinstance(tf15, dict) and "close" in tf15 and len(tf15["close"]) >= 15:
            rsi15 = float(_rsi(tf15["close"], 14)[-1])
        rsi_neutral_lo = float(getattr(C, "TS_RSI15_NEUTRAL_LO", 45.0))
        rsi_neutral_hi = float(getattr(C, "TS_RSI15_NEUTRAL_HI", 55.0))
        rsi_block = False
        allow_long_side = True
        allow_short_side = True
        if rsi15 is not None:
            if rsi_neutral_lo <= rsi15 <= rsi_neutral_hi:
                rsi_block = True
                allow_long_side = False
                allow_short_side = False
            else:
                allow_long_side = rsi15 > 50.0
                allow_short_side = rsi15 < 50.0

        # --- Filter toggles: allow disabling RSI/Regime independently via config ---
        use_rsi_filter = bool(getattr(C, "TS_USE_RSI_FILTER", True))
        use_regime_filter = bool(getattr(C, "TS_USE_REGIME_FILTER", True))

        # If RSI filter is disabled, ignore rsi_block and side-permission; treat as passed
        rsi_gate_long = (not use_rsi_filter) or ((not rsi_block) and allow_long_side)
        rsi_gate_short = (not use_rsi_filter) or ((not rsi_block) and allow_short_side)

        # --- RSI overheat guard (require structural confirmation when stretched) ---
        overheat_hi = float(getattr(C, "TS_RSI_OVERHEAT_HI", 65.0))
        overheat_lo = float(getattr(C, "TS_RSI_OVERHEAT_LO", 35.0))
        force_struct_long = rsi15 is not None and rsi15 >= overheat_hi
        force_struct_short = rsi15 is not None and rsi15 <= overheat_lo

        # 2) ADX(5m) threshold (moved here so EMA/RSI context is available)
        adx_series_14 = _adx(highs, lows, closes, 14)
        adx_last = float(adx_series_14[-1])
        adx_min = float(getattr(C, "TS_ADX_MIN", 20.0))
        # Slope bonus: if ADX rising over the last ~3 bars, allow a small reduction in the minimum
        try:
            adx_slope3 = (
                float(adx_series_14[-1] - adx_series_14[-4]) if len(adx_series_14) >= 4 else 0.0
            )
        except Exception:
            adx_slope3 = 0.0
        adx_slope_bonus = float(getattr(C, "TS_ADX_SLOPE_BONUS", 2.0))
        adx_min_eff = adx_min - (adx_slope_bonus if adx_slope3 > 0.0 else 0.0)
        # strict gate (slope-aware)
        adx_ok_strict = adx_last >= adx_min_eff
        # optional soft override via EMA+RSI alignment
        use_soft = bool(getattr(C, "TS_OVERRIDE_EMA_RSI", False))
        adx_soft_thr = float(getattr(C, "TS_ADX_SOFT", 15.0))
        long_soft_ok = (
            use_soft
            and ma_long_ok
            and (rsi15 is not None and rsi15 > 55.0)
            and (adx_last >= adx_soft_thr)
        )
        short_soft_ok = (
            use_soft
            and ma_short_ok
            and (rsi15 is not None and rsi15 < 45.0)
            and (adx_last >= adx_soft_thr)
        )
        adx_ok = adx_ok_strict or long_soft_ok or short_soft_ok
        # --- ADAPTIVE REGIME MULTIPLIER (added 2025-09-10 08:34 IST) ---
        _adapt_regime = bool(getattr(C, "TS_ADAPT_REGIME", True))
        _base_regime_mult = float(getattr(C, "TS_TL_WIDTH_ATR_MULT", 0.5))
        _adx1 = float(getattr(C, "TS_ADAPT_ADX1", 30.0))
        _adx2 = float(getattr(C, "TS_ADAPT_ADX2", 40.0))
        _mult1 = float(getattr(C, "TS_ADAPT_MULT1", 0.35))  # when ADX >= _adx1
        _mult2 = float(getattr(C, "TS_ADAPT_MULT2", 0.25))  # when ADX >= _adx2

        regime_mult = _base_regime_mult
        if _adapt_regime:
            if adx_last >= _adx2:
                regime_mult = min(regime_mult, _mult2)
            elif adx_last >= _adx1:
                regime_mult = min(regime_mult, _mult1)

        # 5) Regime width: TL channel width vs ATR
        # (Compute quickly using the latest upper/lower from the TL calc we did above)
        tl_width = abs((upper[-1] if upper else price) - (lower[-1] if lower else price))
        # [ADAPTIVE_REGIME][2025-09-10 08:34 IST]
        # base assignment moved above;
        # keep no-op for readability
        regime_mult = regime_mult
        regime_ok = tl_width >= (regime_mult * atr14_last)

        # Apply regime toggle: if disabled, skip regime_ok in pre-gates
        regime_gate = (not use_regime_filter) or regime_ok

        pre_long_gate = vol_ok and adx_ok and regime_gate and ma_long_ok and rsi_gate_long
        pre_short_gate = vol_ok and adx_ok and regime_gate and ma_short_ok and rsi_gate_short

        # Pullback
        atr_last_sig = atr14_last
        base_pb = float(getattr(C, "TS_PULLBACK_PCT", 0.0025))
        adapt_pb = max(
            base_pb, float(0.5 * atr_last_sig / max(1e-9, price))
        )  # opens tolerance during high vol
        near_fast = abs(price - ema_fast[-1]) / max(1e-9, ema_fast[-1]) <= adapt_pb

        # WAI proxy from taser_rules (guarded)
        try:
            from app.taser_rules import _wai_momentum

            wai_long = _wai_momentum(closes, highs, lows, True)
            wai_short = _wai_momentum(closes, highs, lows, False)
        except Exception:
            # fallback: neutral momentum
            wai_long = 1.0
            wai_short = 1.0
        wai_min = float(getattr(C, "TS_WAI_MIN", 0.6))

        # optional avoid-zones (OFF by default)
        avoid_dbg = None
        if bool(getattr(C, "TRENDSCALP_USE_AVOID_ZONES", False)):
            from app.taser_rules import dynamic_avoid_zones, in_zones, vwap

            vwp = None
            try:
                vwp = vwap(highs, lows, closes, vols)[-1]
            except Exception:
                vwp = None
            zones, dbg = dynamic_avoid_zones(tf5, vwp, None, None)
            if in_zones(price, zones):
                return Signal(
                    "NONE",
                    0,
                    0,
                    [],
                    "trendscalp: avoid-zone",
                    {"engine": "trendscalp", "avoid": dbg},
                )
            avoid_dbg = dbg

        require_both = bool(getattr(C, "TS_REQUIRE_BOTH", True))
        # If RSI is overheated in the trade direction, force structural+signal agreement
        require_both_long = require_both or force_struct_long
        require_both_short = require_both or force_struct_short
        not_bearish = (delta_pos is not False) and (oi_up in (True, None))
        not_bullish = (delta_pos is not True) or (oi_up is False)

        long_ok = (
            pre_long_gate
            and near_fast
            and (wai_long >= wai_min)
            and not_bearish
            and (
                (ml_bias == "long" and (upper_break or ema_up))
                if require_both_long
                else ((ml_bias == "long") or upper_break or ema_up)
            )
        )
        short_ok = (
            pre_short_gate
            and near_fast
            and (wai_short >= wai_min)
            and not_bullish
            and (
                (ml_bias == "short" and (lower_break or ema_dn))
                if require_both_short
                else ((ml_bias == "short") or lower_break or ema_dn)
            )
        )

        # Decide a tentative side for re-entry gating
        tentative_side = None
        if long_ok and not short_ok:
            tentative_side = "LONG"
        elif short_ok and not long_ok:
            tentative_side = "SHORT"
        elif ema_up or ema_dn:
            tentative_side = "LONG" if ema_up else "SHORT"

        # B) Price-distance re-entry guard with bar-cooldown
        _cool_bars = int(getattr(C, "REENTRY_COOLDOWN_BARS_5M", 1))  # 1 bar default
        _dist_pct = float(getattr(C, "BLOCK_REENTRY_PCT", 0.0015))  # 0.15% default
        if (
            tentative_side
            and _TS_LAST_ENTRY_PX is not None
            and _TS_LAST_ENTRY_SIDE == tentative_side
        ):
            bars_ago = _bars_since(ts5, _TS_LAST_ENTRY_BAR_TS)
            if bars_ago is None or bars_ago <= _cool_bars:
                # Only enforce distance within cooldown window; outside it, allow
                dist = abs(float(price) - float(_TS_LAST_ENTRY_PX)) / max(
                    1e-9, float(_TS_LAST_ENTRY_PX)
                )
                if dist < _dist_pct:
                    try:
                        telemetry.log(
                            "scan",
                            "REENTRY_BLOCK",
                            "price too close to last entry (BLOCK_REENTRY_PCT)",
                            {"price": float(price), "side": tentative_side},
                        )
                    except Exception:
                        pass
                    return Signal(
                        "NONE",
                        0,
                        0,
                        [],
                        "trendscalp: reentry distance block",
                        {"engine": "trendscalp"},
                    )

        # [PATCH_EMA_RELAX] Expose MA relax knobs in filter_cfg for debugging/telemetry
        # Assemble meta with thresholds (filter_cfg) and live measurements (filter_state)
        meta: Dict[str, Any] = {
            "engine": "trendscalp",
            "price": float(price),
            # legacy top-level fields kept for backward compatibility
            "ml_bias": ml_bias,
            "ml_conf": float(ml_conf),
            "ml_regime": (None if ml_regime is None else str(ml_regime)),
            "ml_sum": ml_sum,
            "upper_break": upper_break,
            "lower_break": lower_break,
            "ema_up": ema_up,
            "ema_dn": ema_dn,
            "ema_fast": float(ema_fast[-1]),
            "ema_slow": float(ema_slow[-1]),
            "tl": {"upper": float(upper[-1]), "lower": float(lower[-1])},
            "avoid": avoid_dbg,
            # new structured diagnostics
            "filter_cfg": {
                "TS_VOL_FLOOR_PCT": float(vol_floor),
                "TS_ADX_MIN": float(adx_min),
                "TS_ADX_SOFT": float(getattr(C, "TS_ADX_SOFT", 15.0)),
                "TS_OVERRIDE_EMA_RSI": bool(getattr(C, "TS_OVERRIDE_EMA_RSI", False)),
                "TS_TL_WIDTH_ATR_MULT": float(regime_mult),
                "TS_REQUIRE_BOTH": bool(require_both),
                "TS_RSI15_NEUTRAL_LO": float(rsi_neutral_lo),
                "TS_RSI15_NEUTRAL_HI": float(rsi_neutral_hi),
                "TS_RSI_OVERHEAT_HI": float(overheat_hi),
                "TS_RSI_OVERHEAT_LO": float(overheat_lo),
                # [PATCH_EMA_RELAX][REMOVE_ON_REVERT]
                "TS_MA_REQUIRE_15M": bool(_ma_require_15m),
                "TS_MA_BUFFER_PCT": float(_ma_buffer),
                "TS_USE_RSI_FILTER": bool(use_rsi_filter),
                "TS_USE_REGIME_FILTER": bool(use_regime_filter),
                # --- Adaptive regime filter knobs (2025-09-10 08:34 IST) ---
                "TS_TL_WIDTH_ATR_MULT_BASE": float(getattr(C, "TS_TL_WIDTH_ATR_MULT", 0.5)),
                "TS_ADAPT_REGIME": bool(getattr(C, "TS_ADAPT_REGIME", True)),
                "TS_ADAPT_ADX1": float(getattr(C, "TS_ADAPT_ADX1", 30.0)),
                "TS_ADAPT_ADX2": float(getattr(C, "TS_ADAPT_ADX2", 40.0)),
                "TS_ADAPT_MULT1": float(getattr(C, "TS_ADAPT_MULT1", 0.35)),
                "TS_ADAPT_MULT2": float(getattr(C, "TS_ADAPT_MULT2", 0.25)),
                "TS_TL_WIDTH_ATR_MULT_EFFECTIVE": float(regime_mult),
            },
            "filter_state": {
                # booleans
                "vol_ok": bool(vol_ok),
                "adx_ok": bool(adx_ok),
                "adx_ok_strict": bool(adx_ok_strict),
                "adx_ok_soft": bool(long_soft_ok or short_soft_ok),
                "regime_ok": bool(regime_ok),
                "ma_long_ok": bool(ma_long_ok),
                "ma_short_ok": bool(ma_short_ok),
                "rsi_block": bool(rsi_block),
                # numbers
                "atr14_last": float(round(atr14_last, 6)),
                "adx_last": float(round(adx_last, 3)),
                "adx_slope3": float(round(adx_slope3, 3)),
                "adx_min_eff": float(round(adx_min_eff, 3)),
                "rsi15": (None if rsi15 is None else float(round(rsi15, 3))),
                "ema200_5": float(ema200_5),
                "ema200_15": (float(ema200_15) if ema200_15 is not None else None),
                "tl_width": float(round(tl_width, 6)),
                # side permissions
                "allow_long_side": bool(allow_long_side),
                "allow_short_side": bool(allow_short_side),
                # trigger/momentum context
                "upper_break": bool(upper_break),
                "lower_break": bool(lower_break),
                "ema_up": bool(ema_up),
                "ema_dn": bool(ema_dn),
                "near_fast": float(round(abs(price - ema_fast[-1]) / max(1e-9, ema_fast[-1]), 6)),
                "wai_long": float(round(wai_long, 4)),
                "wai_short": float(round(wai_short, 4)),
                "ml_bias": ml_bias,
                "ml_regime": (None if ml_regime is None else str(ml_regime)),
                "ml_conf": float(ml_conf),
                "rsi_overheat_long": bool(force_struct_long),
                "rsi_overheat_short": bool(force_struct_short),
            },
        }

        # backward-compat aliases for formatters expecting these names
        meta["filters"] = meta.get("filter_state", {})
        meta["validators"] = meta.get("filter_state", {})

        if not long_ok and not short_ok:
            try:
                filters_state = cast(Dict[str, Any], meta.get("filter_state", {}))
                telemetry.log_filter_block(
                    engine="trendscalp",
                    reason="PINE_PARITY_FILTER_BLOCK",
                    filters=filters_state,
                )
            except Exception:
                pass
            return Signal("NONE", 0, 0, [], "trendscalp: filter block or no setup", meta)

        side = (
            "LONG"
            if long_ok and not short_ok
            else ("SHORT" if short_ok and not long_ok else ("LONG" if ema_up else "SHORT"))
        )

        # Record last entry context for re-entry gating on next scans
        if side in ("LONG", "SHORT"):
            _TS_LAST_ENTRY_PX = float(price)
            _TS_LAST_ENTRY_SIDE = side
            _TS_LAST_ENTRY_BAR_TS = (
                curr_bar_ts if isinstance(curr_bar_ts, int) else _TS_LAST_ENTRY_BAR_TS
            )

        # SL: trendline by default
        if str(getattr(C, "TS_STOP_MODE", "trendline")).lower() == "trendline":
            atr_last = atr14_last
            fee = (
                price
                * float(getattr(C, "FEE_PCT", 0.0005))
                * float(getattr(C, "FEE_PAD_MULT", 2.0))
            )
            pad = max(0.6 * atr_last, fee)
            if side == "LONG":
                sl = float(min(price - pad, meta["tl"]["lower"] - pad))
                lo = price - price * float(getattr(C, "MAX_SL_PCT", 0.0120))
                hi = price - price * float(getattr(C, "MIN_SL_PCT", 0.0045))
                sl = max(min(sl, hi), lo)
            else:
                sl = float(max(price + pad, meta["tl"]["upper"] + pad))
                lo2 = price + price * float(getattr(C, "MIN_SL_PCT", 0.0045))
                hi2 = price + price * float(getattr(C, "MAX_SL_PCT", 0.0120))
                sl = min(max(sl, lo2), hi2)
            sl = round(sl, 4)
        else:
            # structural fallback — lazy imports and conservative clamps if helpers are unavailable
            vwp = None
            try:
                from app.taser_rules import vwap as _vwap

                vwp = _vwap(highs, lows, closes, vols)[-1]
            except Exception:
                vwp = None
            atr30 = _atr(highs, lows, closes, 30)[-1]
            try:
                from app.taser_rules import _structural_sl

                sl = _structural_sl(side, price, vwp, None, None, pdh, pdl, atr30, tf1m)
            except Exception:
                # very conservative clamp if structural helper is not available
                min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
                max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))
                if str(side).upper() == "LONG":
                    lo = price - price * max_pct
                    hi = price - price * min_pct
                    sl = max(min(price, hi), lo)
                else:
                    lo2 = price + price * min_pct
                    hi2 = price + price * max_pct
                    sl = min(max(price, lo2), hi2)

        # TPs — delegated to unified calculator (supports R- and ATR-based ladders + mode adapt)
        from app.tp_calc import compute_tps

        atr30 = _atr(highs, lows, closes, 30)[-1]
        _raw_tps = compute_tps(price, sl, side, float(atr30), float(adx_last), C)
        # Coerce into a flat list of floats for the Signal type
        # (handle dict- or float-shaped returns).
        tps: List[float]
        if isinstance(_raw_tps, list):
            if _raw_tps and isinstance(_raw_tps[0], dict):
                dict_list = cast(List[Dict[str, float]], _raw_tps)
                tps = [float(d.get("px", 0.0)) for d in dict_list]
            else:
                float_list = cast(List[float], _raw_tps)
                tps = [float(x) for x in float_list]
        else:
            tps = []

        # --- Confidence-scaled sizing (suggestion only; execution can honor this) ---
        size_mult_suggested = 1.0
        try:
            if bool(getattr(C, "TS_USE_ML_GATE", False)) and bool(
                getattr(C, "TS_ML_CONF_SIZING", False)
            ):
                slope = float(getattr(C, "TS_ML_CONF_SLOPE", 1.0))
                size_mult_suggested = max(0.5, min(1.5, 1.0 + (float(ml_conf) - 0.5) * slope))
        except Exception:
            size_mult_suggested = 1.0
        try:
            meta["size_mult_suggested"] = float(size_mult_suggested)
        except Exception:
            pass
        reason = (
            f"TrendScalp {ml_bias.upper()} "
            f"{'UPBRK' if upper_break else ''}"
            f"{'DNBRK' if lower_break else ''}"
            f"{' EMAUP' if ema_up else (' EMADN' if ema_dn else '')}"
        )
    return Signal(side, round(price, 4), float(sl), tps, reason, meta)


# ---------- public: trendline trailing (separate from TASER manager) ----------
def scalp_manage(
    price: float,
    side: str,
    entry: float,
    sl: float,
    tps: List[float],
    tf5: Dict[str, List[float]],
    meta: Dict,
) -> Dict:
    closes = tf5["close"]
    highs = tf5["high"]
    lows = tf5["low"]
    # Management knobs / context
    pause_abs_locks = bool(getattr(C, "TRENDSCALP_PAUSE_ABS_LOCKS", True))
    arm_be_r = float(getattr(C, "TS_BE_ARM_R", 0.5))  # arm break-even after 0.5R move
    give_arm_r = float(getattr(C, "TS_GIVEBACK_ARM_R", 1.0))  # enable give-back after >=1.0R
    give_frac = float(getattr(C, "TS_GIVEBACK_FRAC", 0.40))  # 40% peak give-back
    rev_adx_min = float(getattr(C, "TS_REVERSAL_ADX_MIN", 22.0))
    ema200_5 = (
        float(_ema(closes, 200)[-1])
        if len(closes) >= 200
        else float(_ema(closes, min(200, len(closes)))[-1])
    )
    adx_series = _adx(highs, lows, closes, 14)
    adx_last = float(adx_series[-1])

    method = str(getattr(C, "TS_TL_SLOPE_METHOD", "atr")).lower()
    L = int(getattr(C, "TS_TL_LOOKBACK", 14))
    mult = float(getattr(C, "TS_TL_SLOPE_MULT", 1.0))
    upper, lower, upos, dnos = _trendlines(highs, lows, closes, L, method, mult)
    upper_now = float(upper[-1])
    lower_now = float(lower[-1])

    ema_fast = _ema(closes, _env_int("TS_EMA_FAST", 8))
    ema_slow = _ema(closes, _env_int("TS_EMA_SLOW", 20))

    def _s(arr, n):
        n = max(2, min(n, len(arr) - 1))
        return (arr[-1] - arr[-n]) / max(1e-9, n)

    ema_up = ema_fast[-1] > ema_slow[-1] and _s(
        ema_fast, _env_int("TS_TREND_SLOPE_LEN", 25)
    ) >= float(getattr(C, "TS_TREND_SLOPE_MIN", 0.0))
    ema_dn = ema_fast[-1] < ema_slow[-1] and _s(
        ema_fast, _env_int("TS_TREND_SLOPE_LEN", 25)
    ) <= -float(getattr(C, "TS_TREND_SLOPE_MIN", 0.0))

    atr_last = _atr(highs, lows, closes, 14)[-1]
    fee = price * float(getattr(C, "FEE_PCT", 0.0005)) * float(getattr(C, "FEE_PAD_MULT", 2.0))
    pad = max(0.6 * atr_last, fee)
    min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
    max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))
    # absolute profit lock (two-stage: $0.25 internal, Stage2 from SCALP_ABS_LOCK_USD, e.g., $0.50)
    abs_lock_usd = float(getattr(C, "SCALP_ABS_LOCK_USD", 0.0))  # Stage2 target, typically 0.50
    stage1_lock = 0.25  # Stage1 is internal and not configurable by env
    # minimum step for SL changes to avoid spammy no-op updates; reuse for TP de-jitter epsilon
    min_step_abs = float(getattr(C, "TS_MIN_SL_CHANGE_ABS", 0.01))
    tp_eps = float(getattr(C, "TS_MIN_SL_CHANGE_ABS", 0.01))
    # noise-aware trailing guards
    sl_min_step_atr = float(
        getattr(C, "TS_SL_MIN_STEP_ATR", 0.05)
    )  # require >= 0.05*ATR step to move SL
    sl_min_buffer_atr = float(
        getattr(C, "TS_SL_MIN_BUFFER_ATR", 0.15)
    )  # keep at least 0.15*ATR gap from price when tightening

    new_sl = float(sl)
    changed = False
    exit_now = False
    why = []
    lock_stage = 0  # 0=none, 1=$0.25, 2=abs_lock_usd (e.g., $0.50)
    lock_amt = 0.0

    # Use peak/trough to avoid missing the lock when price briefly touched the level between ticks
    try:
        peak_px = float(meta.get("peak_px", price)) if isinstance(meta, dict) else price
    except Exception:
        peak_px = price
    try:
        trough_px = float(meta.get("trough_px", price)) if isinstance(meta, dict) else price
    except Exception:
        trough_px = price

    # For LONG, reference the best price we saw; for SHORT, the lowest
    ref_price_long = max(price, peak_px)
    ref_price_short = min(price, trough_px)

    # --- reversal guards (to avoid zero-PnL flip-flops) ---
    try:
        REV_MIN_R = float(getattr(C, "TS_REVERSAL_MIN_R", 0.15))
    except Exception:
        REV_MIN_R = 0.15

    # R is measured off entry vs current SL reference
    def _move_r(curr_price: float, ref_entry: float, ref_sl: float) -> float:
        return abs(curr_price - ref_entry) / max(1e-9, abs(ref_entry - ref_sl))

    # Reversal confirmation & hysteresis
    use_close = bool(getattr(C, "TS_EXIT_USE_CLOSE", True))
    confirm_n = _env_int("TS_EXIT_CONFIRM_BARS", 1)
    rev_pad_mult = float(getattr(C, "TS_REVERSAL_ATR_PAD", 0.2))

    atr_arr = _atr(highs, lows, closes, 14)

    # --- Regime evaluation (CHOP vs RUNNER) with hysteresis ---
    regime_auto = bool(getattr(C, "TS_REGIME_AUTO", True))
    prev_regime = None
    try:
        if isinstance(meta, dict):
            prev_regime = str(meta.get("regime", None)) if meta.get("regime") else None
    except Exception:
        prev_regime = None

    regime = "CHOP"
    regime_dbg: Dict[str, float] = {}
    if regime_auto:
        regime, regime_dbg = classify_regime(
            adx_series,
            atr_arr,
            closes,
            ema200_5,
            prev_regime,
            adx_up=float(getattr(C, "TS_ADX_UP", 26.0)),
            adx_dn=float(getattr(C, "TS_ADX_DN", 23.0)),
            atr_up=float(getattr(C, "TS_ATR_UP", 0.0040)),
            atr_dn=float(getattr(C, "TS_ATR_DN", 0.0035)),
        )

    # Expose regime diagnostics to meta for telemetry/messaging layers
    try:
        if isinstance(meta, dict):
            meta["regime"] = regime
            meta["regime_dbg"] = regime_dbg
    except Exception:
        pass

    # --- TP progress helpers ---
    tp1_hit = False
    tp2_hit = False
    try:
        if isinstance(tps, list) and len(tps) >= 1:
            tp1 = float(tps[0])
            if str(side).upper() == "LONG":
                tp1_hit = price >= tp1
            else:
                tp1_hit = price <= tp1
        if isinstance(tps, list) and len(tps) >= 2:
            tp2 = float(tps[1])
            if str(side).upper() == "LONG":
                tp2_hit = price >= tp2
            else:
                tp2_hit = price <= tp2
    except Exception:
        tp1_hit = False
        tp2_hit = False

    if str(side).upper() == "LONG":
        cand = lower_now - pad
        lo = entry - entry * max_pct
        hi = entry - entry * min_pct
        cand = max(min(cand, hi), lo)
        if cand > new_sl:
            new_sl = cand
            changed = True
            why.append("trail lower TL")
        # Two-stage absolute profit lock for LONG — paused when Pine-parity mode is active
        if not pause_abs_locks:
            if (ref_price_long - entry) >= stage1_lock:
                be1 = entry + (stage1_lock + fee)
                if be1 > new_sl:
                    new_sl = be1
                    changed = True
                    lock_stage = max(lock_stage, 1)
                    lock_amt = stage1_lock
                    why.append(f"lock ${stage1_lock:.2f}")
            if abs_lock_usd > 0.0 and (ref_price_long - entry) >= abs_lock_usd:
                be2 = entry + (abs_lock_usd + fee)
                if be2 > new_sl:
                    new_sl = be2
                    changed = True
                    lock_stage = 2
                    lock_amt = abs_lock_usd
                    why.append(f"lock ${abs_lock_usd:.2f}")
        # --- BE arming & give-back trailing (LONG) ---
        R0 = max(1e-9, abs(entry - sl))  # reference R from current SL vs entry
        move_from_entry = ref_price_long - entry
        # Arm BE once trade has moved sufficiently
        if move_from_entry >= arm_be_r * R0:
            be_sl = entry + fee
            if be_sl > new_sl:
                new_sl = be_sl
                changed = True
                why.append(f"arm BE @{arm_be_r:.2f}R")
        # Give-back trail after >= give_arm_r*R move
        if move_from_entry >= give_arm_r * R0:
            gb_level = (
                ref_price_long - give_frac * move_from_entry
            )  # keep (1 - give_frac) of peak move
            # keep within structural clamps
            lo = entry - entry * max_pct
            hi = entry - entry * min_pct
            gb_level = max(min(gb_level, hi), lo)
            if gb_level > new_sl:
                new_sl = gb_level
                changed = True
                why.append(f"give-back {int(give_frac * 100)}% of peak move")
        # Reverse only if confirmed TL break (with ATR pad) OR EMA flips down
        n = len(closes)
        px_ref = closes[-1] if use_close else price
        tl_break_now = px_ref < (lower_now - rev_pad_mult * atr_arr[-1])
        if confirm_n > 0:
            ok = True
            for i in range(max(0, n - confirm_n), n):
                if closes[i] >= (lower[i] - rev_pad_mult * atr_arr[i]):
                    ok = False
                    break
            tl_break = ok
        else:
            tl_break = tl_break_now
        if tl_break or ema_dn:
            mr = _move_r(px_ref, entry, sl)
            context_ok = (adx_last >= rev_adx_min) and (
                px_ref <= ema200_5
            )  # only flip short if below 200-EMA(5m) and ADX strong
            if mr >= REV_MIN_R and context_ok:
                try:
                    telemetry.log_reverse(
                        engine="trendscalp",
                        allowed=True,
                        move_r=mr,
                        adx=adx_last,
                        ema200_ok=(px_ref <= ema200_5),
                        tl_confirm_bars=_env_int("TS_EXIT_CONFIRM_BARS", 2),
                        tl_break_atr_mult=float(getattr(C, "TS_REVERSAL_ATR_PAD", 0.2)),
                        why="TL/EMA down confirmed",
                    )
                except Exception:
                    pass
                exit_now = True
                why.append(
                    "reverse: TL/EMA down (confirmed) | "
                    f"moveR={mr:.2f}≥{REV_MIN_R:.2f}, "
                    f"ADX={adx_last:.1f}≥{rev_adx_min}, 200EMA ok"
                )
            else:
                try:
                    telemetry.log_reverse(
                        engine="trendscalp",
                        allowed=False,
                        move_r=mr,
                        adx=adx_last,
                        ema200_ok=(px_ref <= ema200_5),
                        tl_confirm_bars=_env_int("TS_EXIT_CONFIRM_BARS", 2),
                        tl_break_atr_mult=float(getattr(C, "TS_REVERSAL_ATR_PAD", 0.2)),
                        why="mr/ADX/EMA context insufficient",
                    )
                except Exception:
                    pass
                why.append(
                    "no reverse: "
                    f"moveR={mr:.2f}, ADX={adx_last:.1f}, "
                    f"200EMA test={(px_ref <= ema200_5)}"
                )
    else:
        cand = upper_now + pad
        lo = entry + entry * min_pct
        hi = entry + entry * max_pct
        cand = min(max(cand, lo), hi)
        if cand < new_sl:
            new_sl = cand
            changed = True
            why.append("trail upper TL")
        # Two-stage absolute profit lock for SHORT — paused when Pine-parity mode is active
        if not pause_abs_locks:
            if (entry - ref_price_short) >= stage1_lock:
                be1 = entry - (stage1_lock + fee)
                if be1 < new_sl:
                    new_sl = be1
                    changed = True
                    lock_stage = max(lock_stage, 1)
                    lock_amt = stage1_lock
                    why.append(f"lock ${stage1_lock:.2f}")
            if abs_lock_usd > 0.0 and (entry - ref_price_short) >= abs_lock_usd:
                be2 = entry - (abs_lock_usd + fee)
                if be2 < new_sl:
                    new_sl = be2
                    changed = True
                    lock_stage = 2
                    lock_amt = abs_lock_usd
                    why.append(f"lock ${abs_lock_usd:.2f}")
        # --- BE arming & give-back trailing (SHORT) ---
        R0 = max(1e-9, abs(entry - sl))
        move_from_entry = entry - ref_price_short
        if move_from_entry >= arm_be_r * R0:
            be_sl = entry - fee
            if be_sl < new_sl:
                new_sl = be_sl
                changed = True
                why.append(f"arm BE @{arm_be_r:.2f}R")
        if move_from_entry >= give_arm_r * R0:
            gb_level = (
                ref_price_short + give_frac * move_from_entry
            )  # keep (1 - give_frac) of peak move
            lo = entry + entry * min_pct
            hi = entry + entry * max_pct
            gb_level = min(max(gb_level, lo), hi)
            if gb_level < new_sl:
                new_sl = gb_level
                changed = True
                why.append(f"give-back {int(give_frac * 100)}% of peak move")
        n = len(closes)
        px_ref = closes[-1] if use_close else price
        tl_break_now = px_ref > (upper_now + rev_pad_mult * atr_arr[-1])
        if confirm_n > 0:
            ok = True
            for i in range(max(0, n - confirm_n), n):
                if closes[i] <= (upper[i] + rev_pad_mult * atr_arr[i]):
                    ok = False
                    break
            tl_break = ok
        else:
            tl_break = tl_break_now
        if tl_break or ema_up:
            mr = _move_r(px_ref, entry, sl)
            context_ok = (adx_last >= rev_adx_min) and (
                px_ref >= ema200_5
            )  # only flip long if above 200-EMA(5m) and ADX strong
            if mr >= REV_MIN_R and context_ok:
                try:
                    telemetry.log_reverse(
                        engine="trendscalp",
                        allowed=True,
                        move_r=mr,
                        adx=adx_last,
                        ema200_ok=(px_ref >= ema200_5),
                        tl_confirm_bars=_env_int("TS_EXIT_CONFIRM_BARS", 2),
                        tl_break_atr_mult=float(getattr(C, "TS_REVERSAL_ATR_PAD", 0.2)),
                        why="TL/EMA up confirmed",
                    )
                except Exception:
                    pass
                exit_now = True
                why.append(
                    "reverse: TL/EMA up (confirmed) | "
                    f"moveR={mr:.2f}≥{REV_MIN_R:.2f}, "
                    f"ADX={adx_last:.1f}≥{rev_adx_min}, 200EMA ok"
                )
            else:
                try:
                    telemetry.log_reverse(
                        engine="trendscalp",
                        allowed=False,
                        move_r=mr,
                        adx=adx_last,
                        ema200_ok=(px_ref >= ema200_5),
                        tl_confirm_bars=_env_int("TS_EXIT_CONFIRM_BARS", 2),
                        tl_break_atr_mult=float(getattr(C, "TS_REVERSAL_ATR_PAD", 0.2)),
                        why="mr/ADX/EMA context insufficient",
                    )
                except Exception:
                    pass
                why.append(
                    "no reverse: "
                    f"moveR={mr:.2f}, ADX={adx_last:.1f}, "
                    f"200EMA test={(px_ref >= ema200_5)}"
                )

    # --- ML degrade-tighten (optional) ---
    try:
        if bool(getattr(C, "TS_EXIT_DEGRADE_TIGHTEN", False)):

            ml_conf_now: Optional[float] = None
            if isinstance(meta, dict):
                _v = meta.get("ml_conf", None)
                if isinstance(_v, (int, float, str)):
                    try:
                        ml_conf_now = float(_v)
                    except Exception:
                        ml_conf_now = None
            ml_hist = []
            try:
                if isinstance(meta, dict):
                    ml_hist = list(meta.get("ml_conf_hist", []))
            except Exception:
                ml_hist = []
            # Keep a short rolling window
            if ml_conf_now is not None:
                ml_hist.append(ml_conf_now)
                if isinstance(meta, dict):
                    meta["ml_conf_hist"] = ml_hist[-10:]
            # If we have enough history, check drop over last N bars
            N = int(getattr(C, "TS_EXIT_DEGRADE_BARS", 3))
            thr = float(getattr(C, "TS_EXIT_DEGRADE_DELTA", 0.15))
            if len(ml_hist) >= N + 1:
                drop = ml_hist[-N - 1] - ml_hist[-1]
                if drop >= thr:
                    # tighten SL by extra ATR multiplier (noise-aware)
                    extra = float(getattr(C, "TS_EXIT_DEGRADE_ATR_MULT", 0.50)) * atr_last
                    if str(side).upper() == "LONG":
                        cand2 = max(new_sl, min(price - min_pct * entry, new_sl + extra))
                        if cand2 > new_sl:
                            new_sl = cand2
                            changed = True
                            why.append(f"degrade-tighten: conf drop {drop:.2f}≥{thr:.2f}")
                    else:
                        cand2 = min(new_sl, max(price + min_pct * entry, new_sl - extra))
                        if cand2 < new_sl:
                            new_sl = cand2
                            changed = True
                            why.append(f"degrade-tighten: conf drop {drop:.2f}≥{thr:.2f}")
    except Exception:
        pass

    # --- Regime-based exit/partial rules (apply after trail logic, before commit guards) ---
    tp1_partial_frac = float(getattr(C, "TS_PARTIAL_TP1", 0.5))  # 50% default
    exit_on_tp1_override = bool(getattr(C, "TS_EXIT_ON_TP1", False))

    if regime_auto:
        if regime == "CHOP":
            if tp1_hit and (not tp2_hit):
                exit_now = True
                why.append("regime=CHOP: exit at TP1")
        else:  # RUNNER
            if tp1_hit:
                # Ensure BE+fees once TP1 is touched; preserve higher SL if already above
                if str(side).upper() == "LONG":
                    be_sl = entry + fee
                    if be_sl > new_sl:
                        new_sl = be_sl
                        changed = True
                        why.append("runner: BE+ after TP1")
                else:
                    be_sl = entry - fee
                    if be_sl < new_sl:
                        new_sl = be_sl
                        changed = True
                        why.append("runner: BE+ after TP1")
                # Signal partial at TP1 to execution layer via meta
                try:
                    if isinstance(meta, dict):
                        meta["partial_at_tp1"] = True
                        meta["partial_frac"] = float(max(0.0, min(1.0, tp1_partial_frac)))
                except Exception:
                    pass
        # If we were RUNNER and degrade to CHOP before TP2, flatten the remainder
        try:
            if prev_regime == "RUNNER" and regime == "CHOP" and (not tp2_hit):
                exit_now = True
                why.append("regime flip RUNNER->CHOP before TP2: flatten remainder")
        except Exception:
            pass

    # Hard override: force full exit at TP1 irrespective of regime
    if exit_on_tp1_override and tp1_hit and (not tp2_hit):
        exit_now = True
        why.append("TS_EXIT_ON_TP1: exit at TP1")

    # --- Noise-aware commit: ATR-based minimum step and minimum buffer from price ---
    if changed:
        # 1) ATR-scaled minimum step
        min_step_atr_abs = float(sl_min_step_atr * atr_last)
        if abs(new_sl - sl) < max(min_step_abs, min_step_atr_abs):
            # Do not move SL if improvement is too small
            new_sl = sl
            changed = False
            why.append(f"hold: SL delta < max({min_step_abs:.4f}, {min_step_atr_abs:.4f} ATR)")
        else:
            # 2) Maintain a minimum buffer between price and the tightened SL
            min_buffer_abs = float(sl_min_buffer_atr * atr_last)
            if str(side).upper() == "LONG":
                # For LONG, SL sits below price; do not pull it too close
                if (price - new_sl) < min_buffer_abs:
                    new_sl = sl
                    changed = False
                    why.append(f"hold: buffer < {sl_min_buffer_atr:.2f}×ATR")
            else:
                # SHORT: SL sits above price; do not push it too close
                if (new_sl - price) < min_buffer_abs:
                    new_sl = sl
                    changed = False
                    why.append(f"hold: buffer < {sl_min_buffer_atr:.2f}×ATR")

    # Suppress tiny SL changes to reduce telegram/log spam
    if abs(new_sl - sl) < min_step_abs:
        new_sl = sl
        if changed:
            # revert change flag if movement is below threshold
            changed = False
            why.append(f"ignore tiny SL delta (<{min_step_abs:.4f})")

    # --- TP de-jitter & de-dup ---
    # Round proposed TPs and keep the incoming ones if deltas are tiny, to avoid replace spam
    proposed_tps = [float(round(x, 4)) for x in (tps or [])]
    tps_changed = _tp_diff_exceeds((tps or []), proposed_tps, tp_eps)
    final_tps = proposed_tps

    return {
        "sl": float(round(new_sl, 4)),
        "tps": final_tps,
        "changed": bool(changed),
        "why": ", ".join(why) or "no change",
        "exit": bool(exit_now),
        "lock_stage": int(lock_stage),
        "lock_amt": float(lock_amt),
        "tps_changed": bool(tps_changed),
    }
