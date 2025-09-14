# app/taser_rules.py
from dataclasses import dataclass
from typing import Dict, List, Optional
from .indicators import rsi, macd, vwap, anchored_vwap
from .analytics import build_liquidity_heatmap
from . import config as C

@dataclass
class Signal:
    side: str; entry: float; sl: float; tps: List[float]; reason: str; meta: Dict

# ----------------- small helpers -----------------

def prior_day_high_low(tf1h: Dict[str, List[float]], now_ts_ms: int):
    from time import gmtime
    times=tf1h["timestamp"]; highs=tf1h["high"]; lows=tf1h["low"]
    day_now= gmtime(now_ts_ms//1000).tm_yday
    idxs=[i for i,t in enumerate(times) if gmtime(t//1000).tm_yday==day_now-1]
    if not idxs: return None, None
    return max(highs[i] for i in idxs), min(lows[i] for i in idxs)

def last_major_swings(closes: List[float], lookback: int=150):
    start=max(0,len(closes)-lookback); rng=range(start,len(closes))
    lo=min(rng, key=lambda i: closes[i]); hi=max(rng, key=lambda i: closes[i])
    return hi,lo

def _direction_flips(closes):
    flips=0
    for i in range(2,len(closes)):
        up1=closes[i]>=closes[i-1]; up0=closes[i-1]>=closes[i-2]
        if up1!=up0: flips+=1
    return flips

def _band(lo,hi): return (min(lo,hi), max(lo,hi))

def _atr(highs: List[float], lows: List[float], n:int=30)->float:
    k=min(n,len(highs))
    if k==0: return 0.0
    tr=[max(0.0, highs[i]-lows[i]) for i in range(-k,0)]
    return (sum(tr)/len(tr)) if tr else 0.0

def _near_pct(a: float, b: float, pct: float)->bool:
    if a is None or b is None: return False
    return abs(a-b) / max(abs(b),1e-9) <= pct

def _near_dyn(a: float, b: float, atr_pct: float, pct_min: float, pct_max: float, mult: float)->bool:
    if a is None or b is None: return False
    threshold = max(pct_min, mult*atr_pct)
    threshold = min(threshold, pct_max)
    return abs(a-b)/max(abs(b),1e-9) <= threshold

def _aggr_boost(pct_max: float)->float:
    if C.AGGRESSION=="aggressive": return pct_max
    if C.AGGRESSION=="balanced":  return pct_max*0.66
    return pct_max*0.5  # conservative

# [OPPORTUNISTIC_TWEAK] softer orderflow + diagnostics

def _no_trade_tag(price: float, vwp: Optional[float], avhi: Optional[float], avlo: Optional[float],
                  delta_pos: Optional[bool], oi_up: Optional[bool],
                  long_bias: bool, short_bias: bool,
                  avoid_dbg: Optional[Dict]=None) -> str:
    """[OPPORTUNISTIC_TWEAK] one-line diagnostics for NO TRADE; returns empty when toggle is off."""
    if not getattr(C, "OPPORTUNISTIC_TWEAKS", True):
        return ""
    tags = []
    # orderflow
    if delta_pos is False: tags.append("Δ−")
    elif delta_pos is None: tags.append("Δ≈flat")
    if oi_up is False: tags.append("OI↘")
    elif oi_up is None: tags.append("OI≈flat")
    # bias
    if vwp is not None:
        if price < vwp and not long_bias:  tags.append("bias− long")
        if price > vwp and not short_bias: tags.append("bias− short")
    # avoid
    if avoid_dbg:
        if avoid_dbg.get("confluence"):  tags.append("VWAP/AVWAP compressed")
        if avoid_dbg.get("compression"): tags.append("chop")
    return (" [Tags: " + ", ".join(tags) + "]") if tags else ""

def _flow_ok_long(delta_pos: Optional[bool], oi_up: Optional[bool]) -> bool:
    """[OPPORTUNISTIC_TWEAK] Long flow gate; if toggle off, require Δ+ and OI↑."""
    if not getattr(C, "OPPORTUNISTIC_TWEAKS", True):
        return (delta_pos is True) and (oi_up is True)
    aggr = getattr(C, "AGGRESSION", "conservative").lower()
    avoid_on = bool(getattr(C, "DYN_AVOID_ENABLED", True))
    if aggr == "aggressive" or not avoid_on:
        # allow any non-bearish Δ and OI not decreasing
        return (delta_pos is not False) and (oi_up in (True, None))
    if aggr == "balanced":
        # allow neutral flow
        return (delta_pos in (True, None)) and (oi_up in (True, None))
    return (delta_pos is True) and (oi_up is True)

def _flow_ok_short(delta_pos: Optional[bool], oi_up: Optional[bool]) -> bool:
    """[OPPORTUNISTIC_TWEAK] Short flow gate; if toggle off, require Δ− and OI↘."""
    if not getattr(C, "OPPORTUNISTIC_TWEAKS", True):
        return (delta_pos is False) and (oi_up is False)
    aggr = getattr(C, "AGGRESSION", "conservative").lower()
    avoid_on = bool(getattr(C, "DYN_AVOID_ENABLED", True))
    if aggr == "aggressive" or not avoid_on:
        # allow any non-bullish Δ or OI decreasing
        return (delta_pos is not True) or (oi_up is False)
    if aggr == "balanced":
        # allow neutral flow
        return (delta_pos in (False, None)) or (oi_up in (False, None))
    return (delta_pos is False) and (oi_up is False)
# ----------------- TP ordering -----------------

def _order_tps(side: str, tps: List[float]) -> List[float]:
    """
    Strictly monotonic, deduped, 4dp targets.
    LONG  -> ascending;  SHORT -> descending
    """
    if not tps: return []
    arr = [round(float(x), 4) for x in tps if x is not None]
    if not arr: return []
    asc = (str(side).upper()=="LONG")
    arr = sorted(arr) if asc else sorted(arr, reverse=True)
    out: List[float] = []
    last = None
    for x in arr:
        if last is None or (asc and x>last) or ((not asc) and x<last):
            out.append(x); last=x
    return out[:3]

# ----------------- heatmap helpers (light) -----------------

def _hm_levels(tf: Dict[str, List[float]], window:int=180, tick:float=0.05) -> List[Dict[str,float]]:
    try:
        hm = build_liquidity_heatmap(tf, window=window, tick=tick) or {}
        return hm.get("levels") or []
    except Exception:
        return []

def _hm_confluence(price: float, atr_pct: float,
                   lv5: List[Dict[str,float]], lv15: List[Dict[str,float]], lv1h: List[Dict[str,float]],
                   top_n:int=12) -> Dict[str, float]:
    """Count strong walls near price above/below across TFs. Return counts as ints in a dict."""
    tol_pct = max(C.NEAR_VWAP_PCT_MIN, C.ATR_NEAR_MULT * atr_pct)
    tol = price * tol_pct

    def _hits(levels):
        levels = levels[:top_n]
        above = any(abs(float(l.get("px", 0.0)) - price) <= tol and float(l.get("px", 0.0)) >= price for l in levels)
        below = any(abs(float(l.get("px", 0.0)) - price) <= tol and float(l.get("px", 0.0)) <= price for l in levels)
        return int(above), int(below)

    a5,b5   = _hits(lv5)
    a15,b15 = _hits(lv15)
    a1h,b1h = _hits(lv1h)

    return {"tol_pct": float(tol_pct), "hits_above": int(a5+a15+a1h), "hits_below": int(b5+b15+b1h)}

# ----------------- momentum proxy -----------------

def _wai_momentum(closes: List[float], highs: List[float], lows: List[float], is_long: bool) -> float:
    n = min(12, len(closes))
    if n < 4: return 0.0
    hh = ll = 0
    cls = 0.0
    for i in range(-n+1, 0):
        if closes[i] > closes[i-1]: hh += 1
        if closes[i] < closes[i-1]: ll += 1
        rng = max(1e-9, highs[i]-lows[i])
        pos = (closes[i]-lows[i])/rng   # 1=close near high; 0=near low
        cls += pos
    trend = (hh/(n-1)) if is_long else (ll/(n-1))
    loc   = (cls/(n-1)) if is_long else (1.0 - (cls/(n-1)))
    return float(trend + loc)  # ~0..2

def _noise_1m(tf1m: Optional[Dict[str, List[float]]], bars: int) -> float:
    """Median of last N (high-low) 1m bars as micro-noise proxy, returns absolute price units."""
    if not tf1m or not tf1m.get("high") or not tf1m.get("low"):
        return 0.0
    hi_all = tf1m["high"]
    lo_all = tf1m["low"]
    if not hi_all or not lo_all or len(hi_all) != len(lo_all):
        return 0.0
    n_avail = len(hi_all)
    k = max(3, min(int(bars), n_avail))  # at least 3 bars, at most available
    hi = hi_all[-k:]
    lo = lo_all[-k:]
    spans = [max(0.0, float(hi[i]) - float(lo[i])) for i in range(len(hi))]
    spans.sort()
    n = len(spans)
    mid = n // 2
    if n % 2 == 1:
        return float(spans[mid])
    return float(0.5 * (spans[mid - 1] + spans[mid]))

# ----------------- TP/SL guards -----------------

def _tp_guard(side: str, entry: float, sl: float, tps: List[float], atr: float) -> List[float]:
    sideU = str(side).upper()
    eps = 1e-8
    R = max(1e-9, abs(entry - sl))
    gap = max(0.6*float(atr or 0.0), 0.8*R)

    if sideU == "LONG":
        keep = [float(x) for x in (tps or []) if float(x) > entry + eps]
        keep = _order_tps("LONG", keep)
        while len(keep) < 3:
            base = entry if not keep else keep[-1]
            step = max(gap, (keep[-1] - (keep[-2] if len(keep) > 1 else entry))) if keep else gap
            keep.append(round(base + step, 4))
        return _order_tps("LONG", keep[:3])
    else:
        keep = [float(x) for x in (tps or []) if float(x) < entry - eps]
        keep = _order_tps("SHORT", keep)
        while len(keep) < 3:
            base = entry if not keep else keep[-1]
            step = max(gap, ((keep[-2] - keep[-1]) if len(keep) > 1 else (entry - keep[-1]))) if keep else gap
            keep.append(round(base - step, 4))
        return _order_tps("SHORT", keep[:3])
    
def _tp1_abs_distance() -> float:
    """
    Preferred absolute TP1 distance in price units for TASER.
    Avoids new envs; uses C.TP1_ABS if present, else 0.50.
    """
    try:
        return float(getattr(C, "TP1_ABS", 0.50))
    except Exception:
        return 0.50
# ----------------- SL construction -----------------

def _sl_pad(price: float, atr: float, tf1m: Optional[Dict[str, List[float]]] = None) -> float:
    """Blend ATR and micro-noise, then clamp by absolute rails and add fee cushion."""
    # rails
    min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
    max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))
    floor_abs = price * min_pct
    cap_abs   = price * max_pct

    # components
    alpha      = float(getattr(C, "SL_MIX_ALPHA", 0.55))
    atr_mult   = float(getattr(C, "SL_ATR_MULT", 0.80))
    noise_mult = float(getattr(C, "SL_NOISE_MULT", 1.90))
    n_bars     = int(getattr(C, "SL_NOISE_BARS_1M", 10))
    noise_abs  = _noise_1m(tf1m, n_bars) if tf1m else 0.0

    core = alpha * (atr_mult * float(atr or 0.0)) + (1.0 - alpha) * (noise_mult * float(noise_abs))
    core = max(core, floor_abs)               # respect floor
    core = min(core, cap_abs)                 # respect cap

    fee = price * float(getattr(C, "FEE_PCT", 0.0005)) * float(getattr(C, "FEE_PAD_MULT", 2.0))
    pad = max(core, fee, 1e-6)
    return pad

def _structural_sl(side: str, price: float, vwp: Optional[float],
                   avhi: Optional[float], avlo: Optional[float],
                   pdh: Optional[float], pdl: Optional[float],
                   atr: float,
                   tf1m: Optional[Dict[str, List[float]]] = None) -> float:
    """
    Anchor SL beyond nearest structural level ± blended volatility pad,
    then enforce MIN/MAX rails relative to entry.
    """
    pad = _sl_pad(price, atr, tf1m)
    min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
    max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))

    if str(side).upper() == "SHORT":
        refs = [x for x in [pdh, avhi, vwp] if x is not None]
        base = max(refs) if refs else price
        sl = max(price + pad, base + pad)
        # clamp to rails
        lo = price + price*min_pct
        hi = price + price*max_pct
        sl = min(max(sl, lo), hi)
        return round(sl, 4)

    # LONG
    refs = [x for x in [pdl, avlo, vwp] if x is not None]
    base = min(refs) if refs else price
    sl = min(price - pad, base - pad)
    lo = price - price*max_pct
    hi = price - price*min_pct
    sl = max(min(sl, hi), lo)
    return round(sl, 4)

# ----------------- TP builders -----------------

def _make_tps(entry: float, sl: float, atr: float, side: str) -> List[float]:
    side = str(side).upper()
    tp1_abs = _tp1_abs_distance()

    if C.TP_MODE == "atr":
        m1 = float(getattr(C, "TP1_ATR_MULT", 0.90))
        m2 = float(getattr(C, "TP2_ATR_MULT", 1.50))
        m3 = float(getattr(C, "TP3_ATR_MULT", 2.20))
        raw = [entry + m*atr for m in (m1, m2, m3)] if side == "LONG" else [entry - m*atr for m in (m1, m2, m3)]
    else:
        R = max(1e-9, abs(entry - sl))
        mults = list(getattr(C, "TP_R_MULTIS", [1.0, 1.8, 2.6]))
        try:
            mults = [float(x) for x in mults]
        except Exception:
            mults = [1.0, 1.8, 2.6]
        raw = [entry + (m*R) if side == "LONG" else entry - (m*R) for m in mults[:3]]

    # Keep TP1 close (~$0.50) so we can move to BE+fee early and trail the runner.
    if side == "LONG":
        raw[0] = round(entry + tp1_abs, 4)
    else:
        raw[0] = round(entry - tp1_abs, 4)

    return _order_tps(side, raw)

def _enforce_min_r(entry: float, sl: float, tps: List[float], side: str, atr: float) -> List[float]:
    """TP1 must be ≥ MIN_R_MULT * R; stretch if needed and keep others sensible."""
    if not tps: return tps
    R = max(1e-9, abs(entry - sl))
    need = min(float(getattr(C, "MIN_R_MULT", 1.4)) * R, _tp1_abs_distance())
    side = str(side).upper()

    def dist(x): return abs(x - entry)
    if dist(tps[0]) + 1e-12 >= need:
        return _order_tps(side, tps)

    # stretch TP1 & re-space
    if side=="LONG":
        tp1 = round(entry + need, 4)
        gap = max(0.6*atr, 0.8*R)
        tp2 = max(tp1 + gap, (tps[1] if len(tps)>1 else tp1 + 1.2*gap))
        tp3 = max(tp2 + gap, (tps[2] if len(tps)>2 else tp2 + 1.2*gap))
        return _order_tps("LONG", [tp1,tp2,tp3])
    else:
        tp1 = round(entry - need, 4)
        gap = max(0.6*atr, 0.8*R)
        tp2 = min(tp1 - gap, (tps[1] if len(tps)>1 else tp1 - 1.2*gap))
        tp3 = min(tp2 - gap, (tps[2] if len(tps)>2 else tp2 - 1.2*gap))
        return _order_tps("SHORT", [tp1,tp2,tp3])

# ----------------- avoid zones -----------------

def dynamic_avoid_zones(tf5, vwap5_last, avwap_hi_last, avwap_lo_last):
    """Avoid zones only when objective compression or VWAP/AVWAP confluence exists."""
    if not getattr(C, "DYN_AVOID_ENABLED", True):
        return [], {"compression": False, "confluence": False, "flips": 0, "width_pct": 0.0, "spread_pct": None, "atr": None}
    n=min(getattr(C, "AVOID_LOOKBACK_BARS", 120), len(tf5["close"]))
    closes=tf5["close"][-n:]; highs=tf5["high"][-n:]; lows=tf5["low"][-n:]
    cmin,cmax=min(closes),max(closes)
    width_pct=(cmax-cmin)/max(1e-9,closes[-1])
    flips=_direction_flips(closes)
    compression = (flips>=getattr(C, "CHOP_MIN_FLIPS", 12) and width_pct<=getattr(C, "CHOP_MAX_WIDTH_PCT", 0.006))

    zones=[]; spread_pct=None
    V=[v for v in [vwap5_last,avwap_hi_last,avwap_lo_last] if v is not None]
    if len(V)>=2:
        spread_pct=(max(V)-min(V))/max(1e-9,closes[-1])
        if spread_pct<=getattr(C, "CONF_MAX_SPREAD_PCT", 0.004):
            zones.append(_band(min(V),max(V)))

    atr=_atr(highs,lows,30)
    if (compression or (spread_pct is not None and spread_pct<=getattr(C, "CONF_MAX_SPREAD_PCT", 0.004))) and atr>0:
        c=closes[-1]
        zones.append(_band(c-0.35*atr, c+0.35*atr))  # gated, narrower

    zones.sort(key=lambda z:z[0]); merged=[]
    for z in zones:
        if not merged: merged.append(z); continue
        a,b=merged[-1]; c,d=z
        if c<=b: merged[-1]=(a,max(b,d))
        else: merged.append(z)

    debug={"compression": compression, "confluence": (spread_pct is not None and spread_pct<=getattr(C, "CONF_MAX_SPREAD_PCT", 0.004)),
           "flips": flips, "width_pct": round(width_pct,6), "spread_pct": (None if spread_pct is None else round(spread_pct,6)),
           "atr": round(atr,6)}
    return merged, debug

def in_zones(px, zones):
    for lo,hi in zones:
        if lo<=px<=hi: return True
    return False

# ----------------- main signal -----------------

def taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up: Optional[bool], delta_pos: Optional[bool], tf1m: Optional[Dict[str, List[float]]] = None) -> Signal:
    # --- History availability & soft requirements ---
    need5 = 180      # 5m: covers heatmap(180), swings(150), ATR(30)
    need15 = 14      # 15m: RSI(14)
    need1h = 24      # 1h: ~one day for PDH/PDL & heatmap soft requirement

    closes5=tf5["close"]; highs5=tf5["high"]; lows5=tf5["low"]; vols5=tf5["volume"]
    closes15 = tf15["close"]
    # record history counts for meta/debug
    have5 = len(closes5)
    have15 = len(closes15)
    have1h = len(tf1h.get("close", [])) if isinstance(tf1h, dict) else 0

    # RSI with fallback
    rsi15 = rsi(closes15, 14)
    if rsi15 and len(rsi15) >= 1:
        rsi_now = rsi15[-1]
        rsi_tf_used = "15m"
    else:
        # graceful fallback to 5m RSI if 15m history is insufficient
        rsi5 = rsi(closes5, 14)
        rsi_now = (rsi5[-1] if rsi5 else None)
        rsi_tf_used = "5m_fallback"

    macd_line, signal_line, macd_hist = macd(closes5)
    vwap5 = vwap(highs5, lows5, closes5, vols5)
    hi_idx, lo_idx = last_major_swings(closes5, 150)
    avwap_hi = anchored_vwap(highs5, lows5, closes5, vols5, hi_idx)
    avwap_lo = anchored_vwap(highs5, lows5, closes5, vols5, lo_idx)
    avhi = avwap_hi[-1] if avwap_hi and len(avwap_hi) == len(closes5) else None
    avlo = avwap_lo[-1] if avwap_lo and len(avwap_lo) == len(closes5) else None
    vwp = vwap5[-1] if vwap5 else None

    atr = _atr(highs5, lows5, 30)
    atr_pct = atr / max(price, 1e-9)

    noise_abs = _noise_1m(tf1m, int(getattr(C, "SL_NOISE_BARS_1M", 10))) if tf1m else 0.0
    meta_noise = {"noise_1m_abs": round(float(noise_abs), 6), "noise_mult": float(getattr(C, "SL_NOISE_MULT", 1.90))}

    # heatmap windows capped by available history
    win5 = min(180, have5)
    win15 = min(180, have15)
    win1h = min(180, have1h) if have1h else 0
    hm5 = _hm_levels(tf5, window=win5 or 0, tick=0.05)
    hm15 = _hm_levels(tf15, window=win15 or 0, tick=0.05)
    hm1h = _hm_levels(tf1h, window=win1h or 0, tick=0.10)
    hm = _hm_confluence(price, atr_pct, hm5, hm15, hm1h, top_n=12)

    meta = {
        "pdh": pdh,
        "pdl": pdl,
        "vwap5": vwp,
        "avwap_hi": avhi,
        "avwap_lo": avlo,
        "rsi": rsi_now,
        "rsi_tf": rsi_tf_used,
        "macd_hist": macd_hist,
        "oi_up": oi_up,
        "delta_pos": delta_pos,
        "atr": atr,
        "atr_pct": atr_pct,
        "history": {
            "need5": need5, "have5": have5,
            "need15": need15, "have15": have15,
            "need1h": need1h, "have1h": have1h,
        },
        "heatmap_levels_5m": hm5[:24],
        "heatmap_levels_15m": hm15[:24],
        "heatmap_levels_1h": hm1h[:24],
        "hm_confluence": hm,
        "sl_debug": dict(meta_noise),
    }

    zones, dbg = dynamic_avoid_zones(tf5, vwp, avhi, avlo); 
    meta["avoid_zones"]=zones; meta["avoid_debug"]=dbg
    if in_zones(price, zones):
        why="In dynamic avoid/trap zone"
        if dbg.get("confluence"): why+=" (VWAP/AVWAP compressed)"
        elif dbg.get("compression"): why+=" (chop)"
        tag = _no_trade_tag(price, vwp, avhi, avlo, delta_pos, oi_up,
                            long_bias=False, short_bias=False, avoid_dbg=dbg)
        return Signal("NONE",0,0,[],"No edge at actionable levels — "+why+tag,meta)  # [OPPORTUNISTIC_TWEAK]

    # Bias & safety
    long_bias  = (vwp is not None and price >= vwp and (macd_hist is None or macd_hist >= 0))
    short_bias = (vwp is not None and price <= vwp and (macd_hist is None or macd_hist <= 0))
    rsi_fake   = (rsi_now is not None and rsi_now > C.RSI_OB and (macd_hist is not None and macd_hist <= 0))

    # Momentum/absorption proxy (WAI)
    wai_long  = _wai_momentum(closes5, highs5, lows5, True)
    wai_short = _wai_momentum(closes5, highs5, lows5, False)
    meta["wai"] = {"long": round(wai_long,3), "short": round(wai_short,3)}

    wall_up   = hm.get("hits_above", 0) >= 2
    wall_down = hm.get("hits_below", 0) >= 2

    # ---- Decide side + reason (same edges as your original file) ----
    side, reason = "NONE", "No edge at actionable levels"

    # Ensure proximity flags are always defined (avoid UnboundLocalError)
    near_pdh = False
    near_avhi = False
    near_avlo = False
    near_vwap = False

    # 1) PDH breakout long
    if pdh and price > pdh and (delta_pos is True) and (oi_up is True) and not rsi_fake:
        if not (wall_up and wai_long < 1.2):
            side, reason = "LONG", "Reclaim PDH + Δ+ OI↑"

    # 2) AVWAP↑ / PDH rejection short
    if side == "NONE":
        near_pdh  = (pdh and _near_pct(price, pdh, C.NEAR_PDH_PCT))
        near_avhi = (avhi and _near_pct(price, avhi, C.NEAR_AVWAP_PCT))
        if (near_pdh or near_avhi) and (_flow_ok_short(delta_pos, oi_up) or rsi_fake or short_bias):  # [OPPORTUNISTIC_TWEAK]
            if not (wall_down and wai_short < 1.2):
                side, reason = "SHORT", "Rejection near PDH/AVWAP↑ + Δ−/OI↘/bias−"
    # --- Micro-reversal override to avoid fighting fresh 5m flips (no new envs) ---
    def _micro_trend_up(closes: List[float], k: int = 3) -> bool:
        if not closes or len(closes) < k + 1:
            return False
        return all(float(closes[-i]) > float(closes[-i - 1]) for i in range(1, k + 1))

    def _micro_trend_down(closes: List[float], k: int = 3) -> bool:
        if not closes or len(closes) < k + 1:
            return False
        return all(float(closes[-i]) < float(closes[-i - 1]) for i in range(1, k + 1))

    if side == "SHORT" and _micro_trend_up(closes5, 3) and (macd_hist is not None and macd_hist > 0):
        meta["micro_override"] = "skip_short_micro_up"
        return Signal("NONE", 0, 0, [], "Micro-up override — skip fresh SHORT into 5m flip", meta)

    if side == "LONG" and _micro_trend_down(closes5, 3) and (macd_hist is not None and macd_hist < 0):
        meta["micro_override"] = "skip_long_micro_down"
        return Signal("NONE", 0, 0, [], "Micro-down override — skip fresh LONG into 5m flip", meta)
    # 3) AVWAP↓ reclaim long
    if side=="NONE":
        near_avlo = (avlo and _near_pct(price, avlo, C.NEAR_AVWAP_PCT))
        if near_avlo and _flow_ok_long(delta_pos, oi_up) and not rsi_fake and (long_bias or getattr(C, "AGGRESSION", "conservative") != "conservative"):  # [OPPORTUNISTIC_TWEAK]
            if not (wall_up and wai_long < 1.2):
                side, reason = "LONG", "AVWAP↓ reclaim + Δ+ OI↑ + bias+"

    # 4) VWAP reclaim/lose
    if side=="NONE" and vwp:
        vmax = _aggr_boost(C.NEAR_VWAP_PCT_MAX)
        near_vwap = _near_dyn(price, vwp, atr_pct, C.NEAR_VWAP_PCT_MIN, vmax, C.VWAP_RECLAIM_ATR_MULT)
        if near_vwap and (long_bias or getattr(C, "AGGRESSION", "conservative") != "conservative") and _flow_ok_long(delta_pos, oi_up) and not rsi_fake:  # [OPPORTUNISTIC_TWEAK]
            if not (wall_up and wai_long < 1.2):
                side, reason = "LONG", "VWAP reclaim + Δ+ + bias+"
        elif near_vwap and (short_bias or _flow_ok_short(delta_pos, oi_up)):  # [OPPORTUNISTIC_TWEAK]
            if not (wall_down and wai_short < 1.2):
                side, reason = "SHORT", "VWAP fail + Δ−/bias−"

    # 5) PDL sweep & reclaim long
    if side=="NONE" and pdl and price > pdl:
        pierced = any([l < pdl for l in lows5[-3:]])
        if pierced and _flow_ok_long(delta_pos, oi_up) and (long_bias or getattr(C, "AGGRESSION", "conservative") != "conservative") and not rsi_fake:  # [OPPORTUNISTIC_TWEAK]
            if not (wall_up and wai_long < 1.2):
                side, reason = "LONG", "PDL sweep & reclaim + Δ+ + bias+"

    if side == "NONE":
        tag = _no_trade_tag(price, vwp, avhi, avlo, delta_pos, oi_up, long_bias, short_bias, meta.get("avoid_debug"))
        return Signal("NONE",0,0,[],"No edge at actionable levels"+tag,meta)  # [OPPORTUNISTIC_TWEAK]
    # ---- Build SL structurally + blended vol pad, then TPs, then enforce R quality ----
    sl  = _structural_sl(side, price, vwp, avhi, avlo, pdh, pdl, atr, tf1m)
    tps = _make_tps(price, sl, atr, side)
    tps = _enforce_min_r(price, sl, tps, side, atr)
    tps = _tp_guard(side, price, sl, tps, atr)

    # Debug breakdown for SL
    if "sl_debug" in meta:
        min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
        max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))
        floor_abs = price * min_pct
        cap_abs   = price * max_pct
        atr_pad = float(getattr(C, "SL_ATR_MULT", 0.80)) * float(atr or 0.0)
        n_bars = int(getattr(C, "SL_NOISE_BARS_1M", 10))
        noise_abs_dbg = _noise_1m(tf1m, n_bars) if tf1m else 0.0
        noise_pad = float(getattr(C, "SL_NOISE_MULT", 1.90)) * float(noise_abs_dbg)
        fee = price * float(getattr(C, "FEE_PCT", 0.0005)) * float(getattr(C, "FEE_PAD_MULT", 2.0))
        meta["sl_debug"].update({
            "floor_abs": round(float(floor_abs), 6),
            "cap_abs": round(float(cap_abs), 6),
            "atr_pad": round(float(atr_pad), 6),
            "noise_pad": round(float(noise_pad), 6),
            "fee_pad": round(float(fee), 6),
            "sl_final": float(sl)
        })

    # final ordering + rounding guard
    tps = _order_tps(side, [round(float(x), 4) for x in (tps or [])])
    tps = _tp_guard(side, price, sl, tps, atr)

#    return Signal(side, round(price,4), round(float(sl),4), tps, reason, meta)
    return Signal(side, round(price,4), round(float(sl),4), tps, reason, meta)

# ================= FLOW-BASED MANAGEMENT (Dynamic SL & TP movement) =================
# These helpers are optional and can be called by the runtime loop after a signal is live.
# They DO NOT change the initial signal creation. They only move SL forward and re‑space
# targets as price progresses, never loosening risk.

def _fee_pad_only(price: float) -> float:
    """Small fee cushion used for BE moves so fills don't tag the stop instantly."""
    return price * float(getattr(C, "FEE_PCT", 0.0005)) * float(getattr(C, "FEE_PAD_MULT", 2.0))


def _clamp_trail(side: str, entry: float, new_sl: float) -> float:
    """Ensure SL only tightens in the direction of profit and stays within configured rails."""
    min_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
    max_pct = float(getattr(C, "MAX_SL_PCT", 0.0120))
    lo = entry - entry * max_pct
    hi = entry + entry * max_pct
    if str(side).upper() == "LONG":
        # cannot go below entry - max rail; cannot exceed entry + max rail bound
        return float(max(min(new_sl, hi), entry))  # never below entry for trailing (we only tighten)
    else:
        return float(min(max(new_sl, lo), entry))  # never above entry for trailing (we only tighten)


def _structural_trail(side: str, price: float, atr: float, vwp: Optional[float],
                      avhi: Optional[float], avlo: Optional[float],
                      pdh: Optional[float], pdl: Optional[float],
                      tf1m: Optional[Dict[str, List[float]]] = None) -> float:
    """Build a structural trailing stop just inside nearest structure using same pad logic as initial SL."""
    pad = _sl_pad(price, atr, tf1m)
    if str(side).upper() == "LONG":
        # trail below strongest nearby support
        refs = [x for x in [pdl, avlo, vwp] if x is not None]
        base = max([r for r in refs if r <= price], default=(vwp if vwp is not None else price))
        return float(base - pad)
    else:
        # trail above strongest nearby resistance
        refs = [x for x in [pdh, avhi, vwp] if x is not None]
        base = min([r for r in refs if r >= price], default=(vwp if vwp is not None else price))
        return float(base + pad)


def _respaced_tps_after_partial(side: str, entry: float, sl: float, atr: float,
                                tps: List[float], price: float) -> List[float]:
    """
    If TP1 is effectively achieved (price beyond it), drop it and re‑space TP2/TP3 using
    the remaining R/ATR so the runner has room. Keeps monotonic ordering and rails.
    """
    sideU = str(side).upper()
    if not tps:
        return tps
    hit_tp1 = (price >= tps[0]) if sideU == "LONG" else (price <= tps[0])
    if not hit_tp1:
        return _order_tps(sideU, tps)

    # Remove TP1 and rebuild remaining two targets using R multiples with wider gaps
    new_base = price  # lock progress achieved so far
    R = max(1e-9, abs(entry - sl))
    # wider by design so TP2/TP3 are not too tight after BE
    m2 = float(getattr(C, "FLOW_TP2_R_MULT", 1.6))
    m3 = float(getattr(C, "FLOW_TP3_R_MULT", 2.6))

    if sideU == "LONG":
        raw = [new_base + m2 * R, new_base + m3 * R]
    else:
        raw = [new_base - m2 * R, new_base - m3 * R]

    raw = _order_tps(sideU, raw)
    return _tp_guard(sideU, new_base, sl, raw, atr)


def manage_with_flow(price: float,
                     side: str,
                     entry: float,
                     sl: float,
                     tps: List[float],
                     meta: Dict,
                     tf1m: Optional[Dict[str, List[float]]] = None) -> Dict:
    """
    Dynamic manager to be called on each tick while a position is open.
    - Moves SL to BE + fees when progress >= BE trigger (either TP1 tagged or progress ≥ pct of R).
    - After TP1, re‑spaces remaining TPs using wider R multiples.
    - Trails SL to structural levels (VWAP/AVWAP/PDH/PDL) as price advances.

    Returns a dict: {"sl": float, "tps": List[float], "changed": bool, "why": str}
    """
    if not getattr(C, "FLOW_ENABLED", True):
        return {"sl": float(sl), "tps": list(tps or []), "changed": False, "why": "flow disabled"}

    sideU = str(side).upper()
    atr = float(meta.get("atr", 0.0) or 0.0)
    vwp = meta.get("vwap5")
    avhi = meta.get("avwap_hi")
    avlo = meta.get("avwap_lo")
    pdh = meta.get("pdh")
    pdl = meta.get("pdl")

    R = max(1e-9, abs(entry - sl))
    be_pct = float(getattr(C, "FLOW_BE_AT_R_PCT", 0.75))  # move to BE when progress ≥ 0.75R if TP1 not hit

    changed = False
    why = []

    # 1) Break‑even (plus fees) when progress or TP1 hit
    prog_ok = (price - entry) / R if sideU == "LONG" else (entry - price) / R
    tp1_hit = False
    if tps:
        tp1_hit = (price >= tps[0]) if sideU == "LONG" else (price <= tps[0])

    new_sl = float(sl)

    if tp1_hit or (prog_ok >= be_pct):
        fee = _fee_pad_only(entry)
        if sideU == "LONG":
            cand = max(entry + fee, entry)  # BE + fee
            if cand > new_sl:
                new_sl = cand
                changed = True
                why.append("BE+fee after TP1/progress")
        else:
            cand = min(entry - fee, entry)
            if cand < new_sl:
                new_sl = cand
                changed = True
                why.append("BE+fee after TP1/progress")

    # 2) Structural trail as we advance through TP2/TP3 bands
    #    Use slightly stronger trail once we are past TP1 or >1.2R in profit
    if tp1_hit or prog_ok >= 1.2:
        s_trail = _structural_trail(sideU, price, atr, vwp, avhi, avlo, pdh, pdl, tf1m)
        # blend toward structural trail but never loosen; clamp and keep directionality
        if sideU == "LONG" and s_trail > new_sl:
            new_sl = s_trail
            new_sl = _clamp_trail(sideU, entry, new_sl)
            changed = True
            why.append("trail via structure")
        elif sideU == "SHORT" and s_trail < new_sl:
            new_sl = s_trail
            new_sl = _clamp_trail(sideU, entry, new_sl)
            changed = True
            why.append("trail via structure")

    # 3) Re‑space TPs after TP1 fill so runner has room
    new_tps = list(tps or [])
    if tp1_hit:
        new_tps = _respaced_tps_after_partial(sideU, entry, new_sl, atr, new_tps, price)
        if new_tps != (tps or []):
            changed = True
            why.append("re‑space TPs after TP1")

    return {"sl": float(round(new_sl, 4)), "tps": [float(round(x, 4)) for x in new_tps], "changed": changed, "why": ", ".join(why) or "no change"}