# app/managers/trendscalp_fsm.py — TrendScalp FSM orchestrator (proposals only, no side-effects)
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from .. import config as C
from ..components.guards import be_floor, guard_min_gap
from ..components.locks import abs_lock_from_entry, to_tp_lock, trail_fracR
from ..components.tp import clamp_tp1_distance, ensure_order
from ..ml.ml_assist import score_tp1_probability

# --- Entry snapshot helpers (used by the fill path to persist reasons-for-entry) ---


def _pick_adx(d: dict) -> float:
    try:
        for k in ("adx14", "adx", "di_adx_14"):
            v = (d or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return 0.0


def _pick_atr_px(d: dict) -> float:
    try:
        for k in ("atr5", "atr14", "atr"):
            v = (d or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return 0.0


def _pick_ema200(d: dict) -> float | None:
    try:
        for k in ("ema200", "ema200_5m", "ema_200"):
            v = (d or {}).get(k)
            if v is not None:
                return float(v)
    except Exception:
        pass
    return None


def _structure_flag(side_long: bool, d: dict) -> str:
    """Return 'ok' | 'fail' | 'na' from optional structure flags provided by indicators."""
    key = "structure_ok_long" if side_long else "structure_ok_short"
    try:
        if key in (d or {}):
            return "ok" if bool((d or {}).get(key)) else "fail"
    except Exception:
        pass
    return "na"


def build_entry_validity_snapshot(ctx: Context, feats_5m: dict[str, Any]) -> dict[str, Any]:
    """
    Build a compact snapshot of the *reasons for entry* to persist in trade meta at fill time.
    This function is pure (no side effects). Caller should attach it as meta['entry_validity'].
    """
    is_long = ctx.is_long
    adx_e = _pick_adx(feats_5m)
    atr_px = _pick_atr_px(feats_5m)
    atrpct_e = (atr_px / max(1e-9, float(ctx.price))) if float(ctx.price) > 0 else 0.0
    ema200 = _pick_ema200(feats_5m)
    if ema200 is None:
        ema_side = "na"
    else:
        ema_side = "above" if (float(ctx.price) >= float(ema200)) else "below"
    structure_e = _structure_flag(is_long, feats_5m)
    return {
        "side": "LONG" if is_long else "SHORT",
        "adx_e": float(adx_e),
        "atrpct_e": float(atrpct_e),
        "ema200_side_e": ema_side,
        "structure_e": structure_e,
        "ts_e": float((ctx.meta or {}).get("ts", 0.0)) or float(__import__("time").time()),
    }


@dataclass
class Proposal:
    sl: Optional[float]
    tps: List[float]
    why: str


@dataclass
class Context:
    price: float
    side: str  # "LONG" | "SHORT"
    entry: float
    sl: float
    tps: List[float]
    tf1m: dict[str, Any]
    meta: dict[str, Any]

    @property
    def is_long(self) -> bool:
        return self.side.upper() == "LONG"


# --- helpers for adaptive TP logic ---


def _parse_mults(val, fallback: str) -> tuple[float, float, float]:
    """Parse 3 floats from config that may be a comma string or a list/tuple.
    Examples accepted: "0.6,1.0,1.5"  OR  [0.6, 1.0, 1.5]
    """

    def _parts(x) -> list[str]:
        if x is None:
            return []
        if isinstance(x, (list, tuple)):
            return [str(p).strip() for p in x if str(p).strip()]
        s = str(x).strip()
        return [p.strip() for p in s.split(",") if p.strip()]

    parts = _parts(val)
    if len(parts) < 3:
        parts = _parts(fallback)
    try:
        a, b, c = float(parts[0]), float(parts[1]), float(parts[2])
        return a, b, c
    except Exception:
        fb = _parts(fallback)
        return float(fb[0]), float(fb[1]), float(fb[2])


def _detect_regime(price: float, atr5: float, adx14: float | None) -> str:
    """Return 'chop' or 'rally' from ATR% and ADX thresholds."""
    try:
        atr_pct = (atr5 / max(1e-9, price)) if atr5 else 0.0
        chop_atr_max = float(getattr(C, "MODE_CHOP_ATR_PCT_MAX", 0.0025))
        chop_adx_max = float(getattr(C, "MODE_CHOP_ADX_MAX", 25.0))
        if (atr_pct <= chop_atr_max) and ((adx14 or 0.0) <= chop_adx_max):
            return "chop"
        return "rally"
    except Exception:
        return "chop"


# --- structure trailing helpers ---


def _series(d: dict, key: str) -> list:
    try:
        v = d.get(key) if isinstance(d, dict) else None
        return list(v) if isinstance(v, (list, tuple)) else []
    except Exception:
        return []


def _highest(vals: list, n: int) -> float | None:
    if not vals or n <= 0 or len(vals) < n:
        return None
    return float(max(vals[-n:]))


def _lowest(vals: list, n: int) -> float | None:
    if not vals or n <= 0 or len(vals) < n:
        return None
    return float(min(vals[-n:]))


def _rsi_slope(vals: list, n: int = 3) -> float:
    if not vals or len(vals) < n:
        return 0.0
    return float(vals[-1] - vals[-n])


# --- EMA alignment & structure helpers for PEV / recovery ---


def _ema_side_ok(
    price: float, ema: float | None, is_long: bool, tol_pct: float | None = None
) -> bool:
    """Return True if price is on the correct side of EMA200 (within a small tolerance band).
    If ema is None, treat as unknown but not blocking (return True).
    """
    try:
        if ema is None:
            return True
        tol = float(tol_pct) if tol_pct is not None else float(getattr(C, "EMA_TOL_PCT", 0.0015))
        if is_long:
            return (price >= ema) or (abs(price - ema) / max(1e-9, ema) <= tol)
        else:
            return (price <= ema) or (abs(price - ema) / max(1e-9, ema) <= tol)
    except Exception:
        return True


def _swing_levels(tf1m: dict, n: int) -> tuple[float | None, float | None]:
    """Return recent swing high/low over last n bars from tf1m highs/lows."""
    highs = _series(tf1m, "high")
    lows = _series(tf1m, "low")
    return _highest(highs, n), _lowest(lows, n)


def is_hard_invalidation(price: float, is_long: bool, meta: dict, tf1m: dict) -> dict:
    """Composite hard/soft invalidation assessment used by PEV.
    Hard invalidation requires BOTH:
      1) EMA200 side flip against position (5m OR 15m), and
      2) Structure break of recent swing (n bars) with ATR pad.
    Returns a diagnostic dict with keys: hard, ema_side_ok,
    struct, swing_h, swing_l, pad, ema5, ema15
    """
    d: dict[str, Any] = {}
    try:
        ema5 = (meta or {}).get("ema200_5m") or (meta or {}).get("ema200")
        ema15 = (meta or {}).get("ema200_15m")
        ema5 = float(ema5) if ema5 is not None else None
        ema15 = float(ema15) if ema15 is not None else None
        tol = float(getattr(C, "EMA_TOL_PCT", 0.0015))
        ema_ok = _ema_side_ok(price, ema5, is_long, tol) and _ema_side_ok(
            price, ema15, is_long, tol
        )
        d["ema_side_ok"] = bool(ema_ok)
    except Exception:
        d["ema_side_ok"] = True
        ema5, ema15 = None, None

    try:
        atr5 = float((meta or {}).get("atr5", 0.0))
        # Choose structure window to mirror trailing logic
        if (meta or {}).get("hit_tp3", False):
            n = int(getattr(C, "CHAND_N_POST_TP3", 5))
            k = float(getattr(C, "CHAND_K_POST_TP3", 0.6))
        elif (meta or {}).get("hit_tp2", False):
            n = int(getattr(C, "CHAND_N_POST_TP2", 7))
            k = float(getattr(C, "CHAND_K_POST_TP2", 0.8))
        else:
            n = int(getattr(C, "CHAND_N_PRE_TP2", 9))
            k = float(getattr(C, "CHAND_K_PRE_TP2", 1.2))
        swing_h, swing_l = _swing_levels(tf1m, n)
        pad = k * atr5
        struct_ok = True
        if is_long and swing_l is not None:
            struct_ok = not (price < (swing_l - pad))
        elif (not is_long) and swing_h is not None:
            struct_ok = not (price > (swing_h + pad))
        d.update(
            {
                "struct": "ok" if struct_ok else "break",
                "swing_h": swing_h,
                "swing_l": swing_l,
                "pad": pad,
                "ema5": ema5,
                "ema15": ema15,
            }
        )
    except Exception:
        d.update(
            {
                "struct": "na",
                "swing_h": None,
                "swing_l": None,
                "pad": 0.0,
                "ema5": ema5,
                "ema15": ema15,
            }
        )

    d["hard"] = (not d.get("ema_side_ok", True)) and (d.get("struct") == "break")
    return d


def propose(ctx: Context) -> Proposal:
    """Return a tighten-only SL and (optionally) refreshed TPs. No venue/TG side-effects.

    - Keeps TASER/common code intact by only proposing values (caller applies via existing helpers).
    - Uses ML assist (p_tp1) safely; neutral fallback if model missing.
    - Honors existing knobs from config/env where applicable.
    """
    is_long = ctx.is_long

    # Unpack & normalize TPs
    tp1, tp2, tp3 = (list(ctx.tps) + [None, None, None])[:3]
    tp1, tp2, tp3 = ensure_order(tp1, tp2, tp3, is_long)

    # ML assist
    p_tp1 = score_tp1_probability(
        price=ctx.price,
        entry=ctx.entry,
        sl=ctx.sl,
        tp1=tp1,
        meta=ctx.meta,
    )

    # Context features
    atr5 = float((ctx.meta or {}).get("atr5", 0.0))
    adx14 = (ctx.meta or {}).get("adx14")
    try:
        adx14 = float(adx14) if adx14 is not None else None
    except Exception:
        adx14 = None

    hit_tp1 = bool((ctx.meta or {}).get("hit_tp1", False))

    # Pre‑TP1 freeze knobs
    freeze_trail = bool(getattr(C, "GLOBAL_NO_TRAIL_BEFORE_TP1", True)) or bool(
        getattr(C, "TRENDSCALP_PAUSE_ABS_LOCKS", False)
    )

    # Start from current SL
    sl_new = float(ctx.sl)

    # ---------- PRE‑TP1 BEHAVIOR: keep TP1 realistic; avoid SL choke ----------
    if (not hit_tp1) and freeze_trail:
        # Clamp TP1/2/3 to ATR‑seeded ladder so TP1 stays achievable (no widen on restart)
        t1, t2, t3 = clamp_tp1_distance(ctx.entry, ctx.sl, tp1, tp2, tp3, is_long, atr5)
        why = f"preTP1_freeze p_tp1={p_tp1:.2f}"
        out_tps = [x for x in [t1, t2, t3] if x is not None]
        return Proposal(sl=round(sl_new, 4), tps=out_tps, why=why)

    # ---------- POST‑TP1 (or trail allowed) SL management ----------
    bars_since_tp1 = int((ctx.meta or {}).get("bars_since_tp1", 0))
    post_tp1_delay = int(getattr(C, "POST_TP1_SL_DELAY_BARS", 3))

    # 0) Optional shallow lock immediately after TP1 (BE + eps)
    if hit_tp1 and bars_since_tp1 == 0:
        eps = float(getattr(C, "BE_EPS_ATR_MULT", 0.10)) * atr5
        sl_new = be_floor(sl_new, is_long, ctx.entry)
        if is_long:
            sl_new = max(sl_new, ctx.entry + eps)
        else:
            sl_new = min(sl_new, ctx.entry - eps)

    # A) Grace window: do not tighten further for first N bars after TP1
    if hit_tp1 and bars_since_tp1 < post_tp1_delay:
        # Keep TPs maintained but do not move SL more
        t1, t2, t3 = clamp_tp1_distance(ctx.entry, ctx.sl, tp1, tp2, tp3, is_long, atr5)
        why = f"postTP1_grace={bars_since_tp1}/{post_tp1_delay} p_tp1={p_tp1:.2f}"
        out_tps = [x for x in [t1, t2, t3] if x is not None]
        return Proposal(sl=round(sl_new, 4), tps=out_tps, why=why)

    # B) Trailing after grace
    trail_style = str(getattr(C, "TRAIL_STYLE", "fracR"))
    if trail_style == "structure":
        highs = _series(ctx.tf1m, "high")
        lows = _series(ctx.tf1m, "low")
        # Choose structure window & pad by phase
        if (ctx.meta or {}).get("hit_tp2", False):
            n = int(getattr(C, "CHAND_N_POST_TP2", 7))
            k = float(getattr(C, "CHAND_K_POST_TP2", 0.8))
        else:
            n = int(getattr(C, "CHAND_N_PRE_TP2", 9))
            k = float(getattr(C, "CHAND_K_PRE_TP2", 1.2))
        if (ctx.meta or {}).get("hit_tp3", False):
            n = int(getattr(C, "CHAND_N_POST_TP3", 5))
            k = float(getattr(C, "CHAND_K_POST_TP3", 0.6))
        pad = k * atr5
        if is_long:
            ll = _lowest(lows, n)
            if ll is not None:
                sl_new = min(sl_new, ll - pad)
        else:
            hh = _highest(highs, n)
            if hh is not None:
                sl_new = max(sl_new, hh + pad)
    else:
        # Fallback: fracR trail (post‑TP1 tuning)
        mode = str(getattr(C, "TP_LOCK_STYLE", "trail_fracR"))
        if mode == "to_tp1" and tp1:
            sl_new = to_tp_lock(
                sl_new,
                is_long,
                tp1,
                atr_mult=float(getattr(C, "TP1_LOCK_ATR_MULT", 0.25)),
                atr=atr5,
            )
            if tp2:
                sl_new = to_tp_lock(
                    sl_new,
                    is_long,
                    tp2,
                    atr_mult=float(getattr(C, "TP2_LOCK_ATR_MULT", 0.35)),
                    atr=atr5,
                )
        else:
            default_frac1 = float(getattr(C, "TP1_LOCK_FRACR", 0.65))
            base_frac1 = float(getattr(C, "POST_TP1_LOCK_FRACR", default_frac1))
            if tp1:
                sl_new = trail_fracR(
                    sl_new,
                    is_long,
                    ctx.entry,
                    tp1,
                    frac=base_frac1,
                    atr_pad=float(getattr(C, "TP1_LOCK_ATR_MULT", 0.25)) * atr5,
                )
            if tp2:
                frac2 = float(getattr(C, "TP2_LOCK_FRACR", 0.75))
                sl_new = trail_fracR(
                    sl_new,
                    is_long,
                    ctx.entry,
                    tp2,
                    frac=frac2,
                    atr_pad=float(getattr(C, "TP2_LOCK_ATR_MULT", 0.35)) * atr5,
                )

    # 1) Absolute $ lock from entry (if configured) — typically tiny insurance
    abs_lock_usd = float(getattr(C, "SCALP_ABS_LOCK_USD", 0.0))
    mfe_abs = float((ctx.meta or {}).get("mfe_abs", 0.0))
    sl_new = abs_lock_from_entry(sl_new, is_long, ctx.entry, ctx.price, mfe_abs, abs_lock_usd)

    # 2) Trail policy (to_tp or fracR with ML nudge)
    mode = str(getattr(C, "TP_LOCK_STYLE", "trail_fracR"))
    if mode == "to_tp1" and tp1:
        sl_new = to_tp_lock(
            sl_new,
            is_long,
            tp1,
            atr_mult=float(getattr(C, "TP1_LOCK_ATR_MULT", 0.25)),
            atr=atr5,
        )
        if tp2:
            sl_new = to_tp_lock(
                sl_new,
                is_long,
                tp2,
                atr_mult=float(getattr(C, "TP2_LOCK_ATR_MULT", 0.35)),
                atr=atr5,
            )
    else:
        base_frac1 = float(getattr(C, "TP1_LOCK_FRACR", 0.40))
        delta1 = 0.0
        if p_tp1 < 0.35:
            delta1 = +0.15
        elif p_tp1 > 0.70:
            delta1 = -0.10
        frac1 = max(0.20, min(0.80, base_frac1 + delta1))
        if tp1:
            sl_new = trail_fracR(
                sl_new,
                is_long,
                ctx.entry,
                tp1,
                frac=frac1,
                atr_pad=float(getattr(C, "TP1_LOCK_ATR_MULT", 0.25)) * atr5,
            )
        if tp2:
            frac2 = float(getattr(C, "TP2_LOCK_FRACR", 0.75))
            sl_new = trail_fracR(
                sl_new,
                is_long,
                ctx.entry,
                tp2,
                frac=frac2,
                atr_pad=float(getattr(C, "TP2_LOCK_ATR_MULT", 0.35)) * atr5,
            )

    # Optional momentum stall take‑profit near target
    try:
        stall_n = int(getattr(C, "STALL_BARS", 3))
        stall_near = float(getattr(C, "STALL_NEAR_TP_ATR", 0.50)) * atr5
        use_rsi = bool(getattr(C, "STALL_RSI_CONFIRM", True))
        closes = _series(ctx.tf1m, "close")
        rsi14 = _series(ctx.tf1m, "rsi14")
        # Count bars against
        if len(closes) >= stall_n + 1:
            if is_long:
                against = all((closes[-i] > closes[-i - 1]) for i in range(1, stall_n + 1))
            else:
                against = all((closes[-i] < closes[-i - 1]) for i in range(1, stall_n + 1))
        else:
            against = False
        rsi_ok = True
        if use_rsi and rsi14:
            slope = _rsi_slope(rsi14, min(3, len(rsi14)))
            rsi_ok = (slope < 0) if is_long else (slope > 0)
        # Near any remaining TP?
        near = False
        for t in [t1, t2, t3]:
            if t is None:
                continue
            if is_long and (t - ctx.price) <= stall_near and t >= ctx.price:
                near = True
                break
            if (not is_long) and (ctx.price - t) <= stall_near and t <= ctx.price:
                near = True
                break
        if against and rsi_ok and near:
            # Propose immediate take by moving TP1 to market ± eps
            eps = float(getattr(C, "STALL_TP_EPS", 0.02))
            t_take = ctx.price + eps if (not is_long) else ctx.price - eps
            t1 = round(t_take, 4)
    except Exception:
        pass

    # 3) Guard SL by min‑gap and BE after TP1
    sl_new = guard_min_gap(sl_new, is_long, ctx.price, ctx.entry, atr5)
    if hit_tp1 and bool(getattr(C, "LOCK_NEVER_WORSE_THAN_BE", True)):
        sl_new = be_floor(sl_new, is_long, ctx.entry)

    # 4) TP maintenance: clamp base ladder; then adaptive widen **only after TP1**
    t1, t2, t3 = clamp_tp1_distance(ctx.entry, ctx.sl, tp1, tp2, tp3, is_long, atr5)

    adapt_used = "off"
    if hit_tp1 and bool(getattr(C, "MODE_ADAPT_ENABLED", False)) and atr5 > 0.0:
        regime = _detect_regime(ctx.price, atr5, adx14)
        if regime == "chop":
            a1, a2, a3 = _parse_mults(
                getattr(C, "MODE_CHOP_TP_ATR_MULTS", "0.60,1.00,1.50"),
                "0.60,1.00,1.50",
            )
        else:
            a1, a2, a3 = _parse_mults(
                getattr(C, "MODE_RALLY_TP_ATR_MULTS", "0.90,1.60,2.60"),
                "0.90,1.60,2.60",
            )
        # Build adaptive seeds from entry
        _d1, d2, d3 = a1 * atr5, a2 * atr5, a3 * atr5
        if is_long:
            seed2, seed3 = ctx.entry + d2, ctx.entry + d3
            # extend-only for longs
            t2 = max(t2, round(seed2, 4)) if t2 is not None else round(seed2, 4)
            t3 = max(t3, round(seed3, 4)) if t3 is not None else round(seed3, 4)
        else:
            seed2, seed3 = ctx.entry - d2, ctx.entry - d3
            # extend-only for shorts
            t2 = min(t2, round(seed2, 4)) if t2 is not None else round(seed2, 4)
            t3 = min(t3, round(seed3, 4)) if t3 is not None else round(seed3, 4)
        adapt_used = regime

    # Final order/clean
    t1, t2, t3 = ensure_order(t1, t2, t3, is_long)

    why = f"p_tp1={p_tp1:.2f} mode={mode} adapt={adapt_used}"
    out_tps = [x for x in (t1, t2, t3) if x is not None]
    return Proposal(
        sl=round(sl_new, 4),
        tps=out_tps,
        why=why,
    )
