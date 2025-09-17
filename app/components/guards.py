# app/components/guards.py
from __future__ import annotations

import time

from .. import config as C


def be_floor(sl_new: float, is_long: bool, entry: float) -> float:
    fees = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    be = float(entry) * (1.0 + fees) if is_long else float(entry) * (1.0 - fees)
    return max(float(sl_new), be) if is_long else min(float(sl_new), be)


def guard_min_gap(sl: float, is_long: bool, price: float, entry: float, atr: float) -> float:
    try:
        g_atr = float(getattr(C, "SL_MIN_GAP_ATR_MULT", 0.35)) * float(atr or 0.0)
    except Exception:
        g_atr = 0.0
    try:
        g_pct = float(getattr(C, "SL_MIN_GAP_PCT", 0.0012)) * float(price or entry or 1.0)
    except Exception:
        g_pct = 0.0
    gap = max(1e-6, max(g_atr, g_pct))
    return min(sl, float(price) - gap) if is_long else max(sl, float(price) + gap)


# --- TrendScalp-safe SL guard (polarity-aware, min-gap, freeze, tighten-only)


def _def_true(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _min_gap_px(price: float, entry: float, atr: float) -> float:
    try:
        g_atr = float(getattr(C, "SL_MIN_GAP_ATR_MULT", 0.35)) * float(atr or 0.0)
    except Exception:
        g_atr = 0.0
    try:
        g_buf = float(getattr(C, "TS_SL_MIN_BUFFER_ATR", 0.20)) * float(atr or 0.0)
    except Exception:
        g_buf = 0.0
    try:
        g_pct = float(getattr(C, "SL_MIN_GAP_PCT", 0.0012)) * float(price or entry or 1.0)
    except Exception:
        g_pct = 0.0
    return max(1e-6, g_pct, g_atr, g_buf)


def guard_sl(
    sl_candidate: float,
    sl_current: float,
    is_long: bool,
    price: float,
    entry: float,
    atr: float,
    *,
    hit_tp1: bool = False,
    allow_be: bool = False,
) -> float:
    """
    Unified SL guard for TrendScalp (safe for generic use as well):
      - Respects GLOBAL_NO_TRAIL_BEFORE_TP1 / TRENDSCALP_PAUSE_ABS_LOCKS (freeze before TP1)
      - Optional BE allowance (only floor to BE, still clamped to min-gap and tighten-only)
      - Polarity-safe clamp relative to current price with min-gap (ATR/%/buffer)
      - Tighten-only: never loosens the stop
    Returns the final stop-loss price (float).
    """
    try:
        freeze_all = _def_true(getattr(C, "GLOBAL_NO_TRAIL_BEFORE_TP1", True)) or _def_true(
            getattr(C, "TRENDSCALP_PAUSE_ABS_LOCKS", False)
        )
    except Exception:
        freeze_all = True

    # If pre‑TP1 freeze is on, return current SL unless BE explicitly allowed
    if (not hit_tp1) and freeze_all and (not allow_be):
        return float(sl_current)

    # Compute min gap
    mg = _min_gap_px(float(price), float(entry), float(atr or 0.0))

    # Optional BE floor
    sl_target = float(sl_candidate)
    if allow_be:
        sl_target = be_floor(sl_target, is_long, float(entry))

    # Polarity clamp around current price by min-gap
    if is_long:
        sl_clamped = min(sl_target, float(price) - mg)
        # tighten-only for longs (stop can only go up)
        sl_final = max(float(sl_current), sl_clamped)
    else:
        sl_clamped = max(sl_target, float(price) + mg)
        # tighten-only for shorts (stop can only go down)
        sl_final = min(float(sl_current), sl_clamped)

    return float(sl_final)


# --- Post‑Entry Validity Guard (PEV): pre‑TP1 continuation check
# Pure function (except for updating meta['pe_guard']).


def _pick_adx(feats: dict) -> float:
    try:
        for k in ("adx14", "adx", "di_adx_14"):
            v = (feats or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return 0.0


def _pick_atr_px(feats: dict) -> float:
    try:
        for k in ("atr5", "atr14", "atr"):
            v = (feats or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return 0.0


def _pick_ema200(feats: dict) -> float | None:
    try:
        for k in ("ema200", "ema200_5m", "ema_200"):
            v = (feats or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return None


def _structure_ok(side_long: bool, feats: dict) -> bool | None:
    """If structure flags are provided by indicators, honor them; else None (unknown)."""
    try:
        key = "structure_ok_long" if side_long else "structure_ok_short"
        if key in (feats or {}):
            return bool((feats or {}).get(key))
    except Exception:
        pass
    return None


def post_entry_validity(
    side: str,
    price: float,
    feats_5m: dict,
    feats_1m: dict | None,
    meta: dict,
    conf=C,
) -> tuple[str, dict]:
    """
    Evaluate whether the entry reasons still hold **before TP1**.
    Returns (state, diagnostics) where state ∈ {"OK", "WARN", "EXIT"}.
    Updates meta['pe_guard'] with state/warn_since/last_reason/last_eval_ts.
    """
    now_ts = time.time()

    # Thresholds (reuse regime bands with small hard bands)
    ADX_DN = float(getattr(conf, "TS_ADX_DN", 23.0))
    ATR_DN = float(getattr(conf, "TS_ATR_DN", 0.0035))
    HARD_ADX_DELTA = float(getattr(conf, "PEV_HARD_ADX_DELTA", 1.0))
    HARD_ATR_MULT = float(getattr(conf, "PEV_HARD_ATR_MULT", 0.90))
    GRACE_BARS_5M = int(getattr(conf, "PEV_GRACE_BARS_5M", 2))
    GRACE_MIN_S = int(getattr(conf, "PEV_GRACE_MIN_S", 300))
    USE_1M_CONF = bool(getattr(conf, "PEV_USE_1M_CONFIRM", True))
    CONF_1M_BARS = int(getattr(conf, "PEV_CONFIRM_1M_BARS", 3))
    REQ_EMA_SIDE = bool(getattr(conf, "PEV_REQUIRE_EMA_SIDE", True))
    REQ_CLOSE_CONF = bool(getattr(conf, "PEV_REQUIRE_CLOSE_CONF", True))

    is_long = str(side).upper() == "LONG"

    # Current features
    adx = _pick_adx(feats_5m)
    atr_px = _pick_atr_px(feats_5m)
    atr_pct = (atr_px / float(price)) if float(price) > 0 else 0.0
    ema200 = _pick_ema200(feats_5m)
    ema_side_ok = True
    if ema200 is not None:
        ema_side_ok = (
            (float(price) >= float(ema200)) if is_long else (float(price) <= float(ema200))
        )

    struct_flag = _structure_ok(is_long, feats_5m)  # may be None if not provided

    # 1m micro-confirm for hard invalidation (optional)
    one_min_conf_bad = False
    if USE_1M_CONF and feats_1m is not None:
        try:
            # Accept either explicit counter bar count or simple boolean flag
            bad_n = int((feats_1m or {}).get("cons_bad_bars", 0))
            one_min_conf_bad = bad_n >= max(1, CONF_1M_BARS)
        except Exception:
            one_min_conf_bad = bool((feats_1m or {}).get("bad_trend", False))

    # Evaluate states
    soft_degrade = (adx <= ADX_DN) or (atr_pct <= ATR_DN) or (struct_flag is False)
    hard_invalidate = (
        (adx <= (ADX_DN - HARD_ADX_DELTA))
        and (atr_pct <= (ATR_DN * HARD_ATR_MULT))
        and ((not REQ_EMA_SIDE) or (ema_side_ok is False))
        and ((not REQ_CLOSE_CONF) or one_min_conf_bad)
    )

    # Prepare meta state bucket
    pe = dict(meta.get("pe_guard") or {})
    state_prev = pe.get("state")
    warn_since = pe.get("warn_since")

    # Decide
    if hard_invalidate:
        pe.update(
            {
                "state": "EXIT",
                "warn_since": None,
                "last_reason": "hard_invalidate",
                "last_eval_ts": now_ts,
            }
        )
        meta["pe_guard"] = pe
        diag = {
            "adx": round(adx, 3),
            "atr_pct": round(atr_pct, 6),
            "ema_side_ok": bool(ema_side_ok),
            "struct": ("ok" if struct_flag else ("fail" if struct_flag is False else "na")),
            "soft": bool(soft_degrade),
            "hard": True,
        }
        return "EXIT", diag

    if soft_degrade:
        # Start or continue grace window
        if not warn_since:
            warn_since = now_ts
        pe.update(
            {
                "state": "WARN",
                "warn_since": warn_since,
                "last_reason": "soft_degrade",
                "last_eval_ts": now_ts,
            }
        )
        meta["pe_guard"] = pe
        # Compute grace left (approximate in seconds; 5m bars)
        grace_elapsed = max(0.0, now_ts - float(warn_since))
        grace_needed_s = max(float(GRACE_MIN_S), float(GRACE_BARS_5M) * 300.0)
        if grace_elapsed >= grace_needed_s:
            # Timeout without recovery → exit
            pe.update(
                {
                    "state": "EXIT",
                    "warn_since": None,
                    "last_reason": "timeout",
                    "last_eval_ts": now_ts,
                }
            )
            meta["pe_guard"] = pe
            diag = {
                "adx": round(adx, 3),
                "atr_pct": round(atr_pct, 6),
                "ema_side_ok": bool(ema_side_ok),
                "struct": ("ok" if struct_flag else ("fail" if struct_flag is False else "na")),
                "soft": True,
                "hard": False,
                "timeout": True,
            }
            return "EXIT", diag
        else:
            diag = {
                "adx": round(adx, 3),
                "atr_pct": round(atr_pct, 6),
                "ema_side_ok": bool(ema_side_ok),
                "struct": ("ok" if struct_flag else ("fail" if struct_flag is False else "na")),
                "soft": True,
                "hard": False,
                "grace_left_s": round(max(0.0, grace_needed_s - grace_elapsed), 1),
            }
            return "WARN", diag

    # If improved or OK → clear warning
    if state_prev == "WARN":
        pe.update(
            {
                "state": "OK",
                "warn_since": None,
                "last_reason": "recovered",
                "last_eval_ts": now_ts,
            }
        )
        meta["pe_guard"] = pe

    diag = {
        "adx": round(adx, 3),
        "atr_pct": round(atr_pct, 6),
        "ema_side_ok": bool(ema_side_ok),
        "struct": ("ok" if struct_flag else ("fail" if struct_flag is False else "na")),
        "soft": False,
        "hard": False,
    }
    return "OK", diag
