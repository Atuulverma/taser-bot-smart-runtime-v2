# app/taser_rules.py
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from .indicators import rsi, macd, vwap, anchored_vwap
from .analytics import build_liquidity_heatmap
from . import config as C

@dataclass
class Signal:
    side: str; entry: float; sl: float; tps: List[float]; reason: str; meta: Dict

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
    tr=[highs[i]-lows[i] for i in range(-k,0)]
    return sum(tr)/len(tr) if tr else 0.0

def dynamic_avoid_zones(tf5, vwap5_last, avwap_hi_last, avwap_lo_last):
    """Avoid zones only when objective compression or VWAP/AVWAP confluence exists."""
    if not C.DYN_AVOID_ENABLED:
        return [], {"compression": False, "confluence": False, "flips": 0, "width_pct": 0.0, "spread_pct": None, "atr": None}
    n=min(C.AVOID_LOOKBACK_BARS, len(tf5["close"]))
    closes=tf5["close"][-n:]; highs=tf5["high"][-n:]; lows=tf5["low"][-n:]
    cmin,cmax=min(closes),max(closes)
    width_pct=(cmax-cmin)/max(1e-9,closes[-1])
    flips=_direction_flips(closes)
    compression = (flips>=C.CHOP_MIN_FLIPS and width_pct<=C.CHOP_MAX_WIDTH_PCT)

    zones=[]; spread_pct=None
    V=[v for v in [vwap5_last,avwap_hi_last,avwap_lo_last] if v is not None]
    if len(V)>=2:
        spread_pct=(max(V)-min(V))/max(1e-9,closes[-1])
        if spread_pct<=C.CONF_MAX_SPREAD_PCT:
            zones.append(_band(min(V),max(V)))

    atr=_atr(highs,lows,30)
    if (compression or (spread_pct is not None and spread_pct<=C.CONF_MAX_SPREAD_PCT)) and atr>0:
        c=closes[-1]
        zones.append(_band(c-0.35*atr, c+0.35*atr))  # gated, narrower

    zones.sort(key=lambda z:z[0]); merged=[]
    for z in zones:
        if not merged: merged.append(z); continue
        a,b=merged[-1]; c,d=z
        if c<=b: merged[-1]=(a,max(b,d))
        else: merged.append(z)

    debug={"compression": compression, "confluence": (spread_pct is not None and spread_pct<=C.CONF_MAX_SPREAD_PCT),
           "flips": flips, "width_pct": round(width_pct,6), "spread_pct": (None if spread_pct is None else round(spread_pct,6)),
           "atr": round(atr,6)}
    return merged, debug

def in_zones(px, zones):
    for lo,hi in zones:
        if lo<=px<=hi: return True
    return False

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

# -----------------------
# Heatmap helpers (light)
# -----------------------
def _hm_levels(tf: Dict[str, List[float]], window:int=180, tick:float=0.05) -> List[Dict[str,float]]:
    try:
        hm = build_liquidity_heatmap(tf, window=window, tick=tick) or {}
        return hm.get("levels") or []
    except Exception:
        return []

def _hm_confluence(price: float, atr_pct: float,
                   lv5: List[Dict[str,float]], lv15: List[Dict[str,float]], lv1h: List[Dict[str,float]],
                   top_n:int=12) -> Dict[str, int | float]:
    """Count strong walls near price above/below across TFs."""
    # tolerance derives from your existing config (no new envs)
    tol_pct = max(C.NEAR_VWAP_PCT_MIN, C.ATR_NEAR_MULT * atr_pct)
    tol = price * tol_pct

    def _hits(levels):
        levels = levels[:top_n]
        above = any(abs(float(l["px"]) - price) <= tol and float(l["px"]) >= price for l in levels)
        below = any(abs(float(l["px"]) - price) <= tol and float(l["px"]) <= price for l in levels)
        return int(above), int(below)

    a5,b5   = _hits(lv5)
    a15,b15 = _hits(lv15)
    a1h,b1h = _hits(lv1h)

    return {
        "tol_pct": tol_pct,
        "hits_above": a5 + a15 + a1h,
        "hits_below": b5 + b15 + b1h
    }

def _wai_momentum(closes: List[float], highs: List[float], lows: List[float], is_long: bool) -> float:
    """
    Very light 'absorption/momentum' proxy:
      - fraction of last N bars making HH (long) or LL (short)
      - plus close-location strength vs range
    Returns ~0..2 range (>=1.2 means strong).
    """
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

# -----------------------
# Main signal
# -----------------------
def taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up: Optional[bool], delta_pos: Optional[bool]) -> Signal:
    closes5=tf5["close"]; highs5=tf5["high"]; lows5=tf5["low"]; vols5=tf5["volume"]
    rsi5=rsi(closes5,14); rsi_now=rsi5[-1]
    _, _, macd_hist = macd(closes5)
    vwap5=vwap(highs5,lows5,closes5,vols5)
    hi_idx, lo_idx = last_major_swings(closes5,150)
    avwap_hi=anchored_vwap(highs5,lows5,closes5,vols5,hi_idx)
    avwap_lo=anchored_vwap(highs5,lows5,closes5,vols5,lo_idx)
    avhi = avwap_hi[-1] if avwap_hi and len(avwap_hi)==len(closes5) else None
    avlo = avwap_lo[-1] if avwap_lo and len(avwap_lo)==len(closes5) else None
    vwp  = vwap5[-1] if vwap5 else None

    atr = _atr(highs5,lows5,30)
    atr_pct = atr / max(price, 1e-9)

    # --- embed heatmap (local TFs) into meta for auditor/dashboard ---
    hm5  = _hm_levels(tf5,  window=180, tick=0.05)
    hm15 = _hm_levels(tf15, window=180, tick=0.05)
    hm1h = _hm_levels(tf1h, window=180, tick=0.10)
    hm = _hm_confluence(price, atr_pct, hm5, hm15, hm1h, top_n=12)

    meta={"pdh":pdh,"pdl":pdl,"vwap5":vwp,"avwap_hi": avhi,"avwap_lo": avlo,
          "rsi": rsi_now, "macd_hist": macd_hist, "oi_up":oi_up, "delta_pos":delta_pos,
          "atr": atr, "atr_pct": atr_pct,
          "heatmap_levels_5m": hm5[:24], "heatmap_levels_15m": hm15[:24], "heatmap_levels_1h": hm1h[:24],
          "hm_confluence": hm}

    zones, dbg = dynamic_avoid_zones(tf5, vwp, avhi, avlo); 
    meta["avoid_zones"]=zones; meta["avoid_debug"]=dbg
    if in_zones(price, zones):
        why="In dynamic avoid/trap zone"
        if dbg.get("confluence"): why+=" (VWAP/AVWAP compressed)"
        elif dbg.get("compression"): why+=" (chop)"
        return Signal("NONE",0,0,[],why,meta)

    # Bias & safety
    long_bias  = (vwp is not None and price >= vwp and (macd_hist is None or macd_hist >= 0))
    short_bias = (vwp is not None and price <= vwp and (macd_hist is None or macd_hist <= 0))
    rsi_fake   = (rsi_now is not None and rsi_now > C.RSI_OB and (macd_hist is not None and macd_hist <= 0))

    # Momentum/absorption proxy (WAI)
    wai_long  = _wai_momentum(closes5, highs5, lows5, True)
    wai_short = _wai_momentum(closes5, highs5, lows5, False)
    meta["wai"] = {"long": round(wai_long,3), "short": round(wai_short,3)}

    # Heatmap wall gating (local confluence; scheduler also does multi-TF with 1d/30d)
    # If we are right under multi-TF resistance (LONG) or above support (SHORT),
    # require stronger confirmation (WAI >= 1.2 + bias + delta/oi).
    wall_up   = hm.get("hits_above", 0) >= 2   # stacked resistance nearby
    wall_down = hm.get("hits_below", 0) >= 2   # stacked support nearby

    # Eligibility (for debug in NO TRADE)
    meta["eligibility"] = {
        "dist_to_vwap_pct": (abs(price-vwp)/price if vwp else None),
        "dist_to_avhi_pct": (abs(price-avhi)/price if avhi else None),
        "dist_to_avlo_pct": (abs(price-avlo)/price if avlo else None),
        "long_bias": bool(long_bias),
        "short_bias": bool(short_bias),
        "near_res_wall": bool(wall_up),
        "near_sup_wall": bool(wall_down),
    }

    # 1) PDH breakout long (conservative)
    if pdh and price > pdh and (delta_pos is True) and (oi_up is True) and not rsi_fake:
        # if stacked resistance directly above, insist on momentum absorption
        if wall_up and wai_long < 1.2:
            return Signal("NONE",0,0,[],"Heatmap resistance confluence — need absorption", meta)
        base=[x for x in [vwp,pdh] if x is not None]
        sl=min(base)*0.996 if base else price*(1-0.004)
        rr=price-sl; tps=[round(price+m*rr,4) for m in C.TP_R_MULTIS]
        return Signal("LONG", round(price,4), round(sl,4), tps, "Reclaim PDH + Δ+ OI↑", meta)

    # 2) AVWAP↑ / PDH rejection short
    near_pdh  = (pdh and _near_pct(price, pdh, C.NEAR_PDH_PCT))
    near_avhi = (avhi and _near_pct(price, avhi, C.NEAR_AVWAP_PCT))
    if (near_pdh or near_avhi) and ((delta_pos is False) or (oi_up is False) or rsi_fake or short_bias):
        if wall_down and wai_short < 1.2:
            return Signal("NONE",0,0,[],"Heatmap support confluence — need absorption", meta)
        sl=price*1.004; rr=sl-price; tps=[round(price-m*rr,4) for m in C.TP_R_MULTIS]
        return Signal("SHORT", round(price,4), round(sl,4), tps, "Rejection near PDH/AVWAP↑ + Δ−/OI↘/bias−", meta)

    # 3) AVWAP↓ reclaim long
    near_avlo = (avlo and _near_pct(price, avlo, C.NEAR_AVWAP_PCT))
    if near_avlo and (delta_pos is True) and (oi_up is True) and not rsi_fake and long_bias:
        if wall_up and wai_long < 1.2:
            return Signal("NONE",0,0,[],"Heatmap resistance confluence — need absorption", meta)
        base=[x for x in [avlo, vwp] if x is not None]
        sl=min(base)*0.996 if base else price*(1-0.004)
        rr=price-sl; tps=[round(price+m*rr,4) for m in C.TP_R_MULTIS]
        return Signal("LONG", round(price,4), round(sl,4), tps, "AVWAP↓ reclaim + Δ+ OI↑ + bias+", meta)

    # 4) VWAP reclaim/lose (trend continuation) — adaptive & aggression-aware
    if vwp:
        vmax = _aggr_boost(C.NEAR_VWAP_PCT_MAX)
        near_vwap = _near_dyn(price, vwp, atr_pct, C.NEAR_VWAP_PCT_MIN, vmax, C.VWAP_RECLAIM_ATR_MULT)
        if near_vwap and long_bias and (delta_pos is True) and not rsi_fake:
            if wall_up and wai_long < 1.2:
                return Signal("NONE",0,0,[],"Heatmap resistance confluence — need absorption", meta)
            base=[x for x in [vwp, avlo] if x is not None]
            sl=min(base)*0.996 if base else price*(1-0.004)
            rr=price-sl; tps=[round(price+m*rr,4) for m in C.TP_R_MULTIS]
            return Signal("LONG", round(price,4), round(sl,4), tps, "VWAP reclaim + Δ+ + bias+", meta)
        if near_vwap and (short_bias or (delta_pos is False)):
            if wall_down and wai_short < 1.2:
                return Signal("NONE",0,0,[],"Heatmap support confluence — need absorption", meta)
            sl=price*1.004; rr=sl-price; tps=[round(price-m*rr,4) for m in C.TP_R_MULTIS]
            return Signal("SHORT", round(price,4), round(sl,4), tps, "VWAP fail + Δ−/bias−", meta)

    # 5) PDL sweep & reclaim long
    if pdl and price > pdl:
        pierced = any([l < pdl for l in lows5[-3:]])
        if pierced and (delta_pos is True) and long_bias and not rsi_fake:
            if wall_up and wai_long < 1.2:
                return Signal("NONE",0,0,[],"Heatmap resistance confluence — need absorption", meta)
            base=[x for x in [pdl, vwp] if x is not None]
            sl=min(base)*0.996 if base else price*(1-0.004)
            rr=price-sl; tps=[round(price+m*rr,4) for m in C.TP_R_MULTIS]
            return Signal("LONG", round(price,4), round(sl,4), tps, "PDL sweep & reclaim + Δ+ + bias+", meta)

    return Signal("NONE",0,0,[],"No edge at actionable levels",meta)