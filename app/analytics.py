# app/analytics.py
from typing import Dict, List, Any, Tuple
import math

# -----------------------------
# Config (safe fallbacks)
# -----------------------------
try:
    # All keys are optional; defaults below are used if missing.
    from . import config as C  # type: ignore
except Exception:  # pragma: no cover
    class C:  # minimal safe defaults if config import fails
        HM_BIN_PCT_MIN = 0.0005         # 0.05%
        HM_BIN_ATR_FRAC = 0.25          # bin width also scales with ATR%
        HM_DWELL_ALPHA = 0.70           # how much dwell time matters vs volume/range
        HM_HALF_LIFE_5M = 120           # bars (5m)
        HM_HALF_LIFE_15M = 120          # bars (15m)
        HM_HALF_LIFE_1H = 96            # bars (1h)
        HM_HALF_LIFE_1D = 30            # bars (1d)
        HM_TOP_K = 24
        HM_MIN_SPACING_BINS = 2         # merge bins within this many ticks
        HEATMAP_RETENTION_DAYS = 90     # used elsewhere

# -----------------------------
# Small utils
# -----------------------------
def _safe_list(d: Dict[str, List[float]], k: str) -> List[float]:
    v = d.get(k)
    return v if isinstance(v, list) else []

def _atr_proxy(highs: List[float], lows: List[float], n: int) -> float:
    """Simple ATR proxy = mean(high-low) over last n bars."""
    k = min(n, len(highs), len(lows))
    if k <= 0: return 0.0
    s = 0.0
    for i in range(-k, 0):
        s += max(0.0, float(highs[i]) - float(lows[i]))
    return s / k

def _atr_pct(px: float, atr: float) -> float:
    if px <= 0: return 0.0
    return atr / px

def _adaptive_tick(last_px: float, atr_pct: float) -> float:
    """
    Adaptive bin size:
      tick_pct = max(HM_BIN_PCT_MIN, HM_BIN_ATR_FRAC * atr_pct)
      tick     = round_to_cents(last_px * tick_pct)
    """
    try:
        base_pct = float(getattr(C, "HM_BIN_PCT_MIN", 0.0005))
        atr_frac = float(getattr(C, "HM_BIN_ATR_FRAC", 0.25))
    except Exception:
        base_pct, atr_frac = 0.0005, 0.25
    pct = max(base_pct, atr_frac * max(0.0, atr_pct))
    raw = max(1e-6, last_px * pct)
    # round to a sensible step (0.01)
    step = 0.01
    return max(step, round(math.floor(raw / step) * step, 6))

def _bin_price(px: float, tick: float) -> float:
    if tick <= 0: tick = 0.01
    return round(math.floor(px / tick) * tick, 6)

def _decay_weight(age_bars: int, half_life_bars: float) -> float:
    """Exponential half-life decay weight; newer bars weigh more."""
    if half_life_bars is None or half_life_bars <= 0:
        return 1.0
    return 0.5 ** (age_bars / float(half_life_bars))

def _merge_nearby(sorted_levels: List[Tuple[float, float]],
                  min_spacing_bins: int = 2,
                  tick: float = 0.01) -> List[Tuple[float, float]]:
    """
    Merge neighboring bins that are within `min_spacing_bins * tick`.
    Input & output: list of (px, score) sorted by px ascending/descending (any).
    """
    if not sorted_levels: return []
    # Sort by price ascending for merging
    levels = sorted(sorted_levels, key=lambda x: x[0])
    out: List[Tuple[float, float]] = []
    cluster_px = levels[0][0]
    cluster_score = levels[0][1]
    span = max(1, int(min_spacing_bins)) * max(tick, 1e-9)

    for px, sc in levels[1:]:
        if abs(px - cluster_px) <= span:
            # merge into cluster
            # keep score as sum, center price as weighted average by score
            total = cluster_score + sc
            if total > 0:
                cluster_px = (cluster_px * cluster_score + px * sc) / total
            cluster_score = total
        else:
            out.append((round(cluster_px, 6), float(cluster_score)))
            cluster_px, cluster_score = px, sc
    out.append((round(cluster_px, 6), float(cluster_score)))
    # Return sorted by score descending for “levels”
    return out

# -----------------------------
# Core builder
# -----------------------------
def _levels_core(tf: Dict[str, List[float]],
                 window: int,
                 tick: float,
                 half_life_bars: float,
                 top_k: int,
                 dwell_alpha: float,
                 min_spacing_bins: int) -> Dict[str, Any]:
    """
    Compute:
      - Score per price bin with decay:
          score_i = (vol_i^alpha) * ((1/range_i)^(1-alpha)) * decay(age_i)
        * alpha tunes volume vs dwell (range-inverse).
      - Compact histogram (px-desc).
      - Top levels (after clustering nearby bins).
    """
    closes = _safe_list(tf, "close")
    highs  = _safe_list(tf, "high")
    lows   = _safe_list(tf, "low")
    vols   = _safe_list(tf, "volume")
    if not closes or not highs or not lows:
        return {"levels": [], "hist": [], "window": 0, "tick": tick}

    n = min(window, len(closes), len(highs), len(lows))
    if n <= 0:
        return {"levels": [], "hist": [], "window": 0, "tick": tick}
    # fill volume if absent
    if not vols or len(vols) < len(closes):
        vols = [1.0] * len(closes)

    # Accumulate into bins
    acc: Dict[float, float] = {}
    alpha = max(0.0, min(1.0, float(dwell_alpha)))
    for i in range(-n, 0):
        # age: 0 for most recent bar
        age = (n - 1) - (i + n)  # convert negative index to 0..n-1
        decay = _decay_weight(age, half_life_bars)
        rng = max(1e-9, float(highs[i]) - float(lows[i]))
        vol = max(0.0, float(vols[i]))
        # blend between volume dominance and dwell dominance
        # dwell proxy = 1/range (narrower ranges = more dwell/congestion)
        base = (vol ** alpha) * ((1.0 / rng) ** (1.0 - alpha))
        score = base * decay
        key = _bin_price(float(closes[i]), tick)
        acc[key] = acc.get(key, 0.0) + score

    # Full histogram (sorted by price DESC for UI)
    hist = sorted([{"px": float(k), "score": float(v)} for k, v in acc.items()],
                  key=lambda x: x["px"], reverse=True)

    # Cluster nearby bins → levels
    merged = _merge_nearby([(k, v) for k, v in acc.items()],
                           min_spacing_bins=min_spacing_bins,
                           tick=tick)
    merged.sort(key=lambda kv: kv[1], reverse=True)
    levels = [{"px": float(px), "score": float(sc)} for px, sc in merged[:max(1, top_k)]]

    return {
        "levels": levels,
        "hist": hist[:240],          # compact histogram slice for UI
        "window": n,
        "tick": tick
    }

# -----------------------------
# Public API (backward-compatible)
# -----------------------------
def build_liquidity_heatmap(tf: Dict[str, List[float]],
                            window: int = 180,
                            tick: float = None) -> Dict[str, Any]:
    """
    Single-TF heatmap with adaptive default tick:
      - If tick=None, it’s computed from price *and* ATR%.
      - Includes 'levels' (clustered top K) and 'hist' (px-desc).
    """
    closes = _safe_list(tf, "close")
    highs  = _safe_list(tf, "high")
    lows   = _safe_list(tf, "low")
    if not closes or not highs or not lows:
        return {"levels": [], "hist": [], "window": 0, "tick": 0.01}

    last_px = float(closes[-1])
    atr = _atr_proxy(highs, lows, min(60, len(highs)))
    tick_auto = _adaptive_tick(last_px, _atr_pct(last_px, atr))
    if tick is None or tick <= 0:
        tick = tick_auto

    # Single-TF half-life: use 5m profile as a decent default
    hl = float(getattr(C, "HM_HALF_LIFE_5M", 120))
    top_k = int(getattr(C, "HM_TOP_K", 24))
    dwell_alpha = float(getattr(C, "HM_DWELL_ALPHA", 0.70))
    min_spacing = int(getattr(C, "HM_MIN_SPACING_BINS", 2))

    return _levels_core(tf, window, tick, hl, top_k, dwell_alpha, min_spacing)

def build_liquidity_heatmap_multi(
    tf5: Dict[str, List[float]] = None,
    tf15: Dict[str, List[float]] = None,
    tf1h: Dict[str, List[float]] = None,
    tf1d: Dict[str, List[float]] = None,
    tf30d: Dict[str, List[float]] = None,
    tick_5m: float = None,
    tick_15m: float = None,
    tick_1h: float = None,
    tick_1d: float = None,
) -> Dict[str, Any]:
    """
    Multi-TF version. Per-TF half-life:
      5m  → HM_HALF_LIFE_5M
      15m → HM_HALF_LIFE_15M
      1h  → HM_HALF_LIFE_1H
      1d  → HM_HALF_LIFE_1D
    Tick sizes are adaptive if not provided.
    """
    out: Dict[str, Any] = {}
    top_k = int(getattr(C, "HM_TOP_K", 24))
    dwell_alpha = float(getattr(C, "HM_DWELL_ALPHA", 0.70))
    min_spacing = int(getattr(C, "HM_MIN_SPACING_BINS", 2))

    def _prep(tf, default_hl, tick_hint):
        if not tf: 
            return None
        closes = _safe_list(tf, "close")
        highs  = _safe_list(tf, "high")
        lows   = _safe_list(tf, "low")
        if not closes or not highs or not lows:
            return {"levels": [], "hist": [], "window": 0, "tick": 0.01}
        last_px = float(closes[-1])
        atr = _atr_proxy(highs, lows, min(60, len(highs)))
        tick_final = tick_hint if (tick_hint and tick_hint > 0) else _adaptive_tick(last_px, _atr_pct(last_px, atr))
        return _levels_core(tf, 180, tick_final, default_hl, top_k, dwell_alpha, min_spacing)

    if tf5:
        out["5m"] = _prep(tf5, float(getattr(C, "HM_HALF_LIFE_5M", 120)), tick_5m)
    if tf15:
        out["15m"] = _prep(tf15, float(getattr(C, "HM_HALF_LIFE_15M", 120)), tick_15m)
    if tf1h:
        out["1h"] = _prep(tf1h, float(getattr(C, "HM_HALF_LIFE_1H", 96)), tick_1h)
    if tf1d:
        out["1d"] = _prep(tf1d, float(getattr(C, "HM_HALF_LIFE_1D", 30)), tick_1d)
    if tf30d:
        # 30d synthetic (from 1h or daily): use 1d half-life, wider binning = adaptive tick via ATR on supplied tf
        out["30d"] = _prep(tf30d, float(getattr(C, "HM_HALF_LIFE_1D", 30)), tick_1d)

    return out