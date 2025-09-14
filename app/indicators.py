from typing import List, Optional, Tuple
"""Exponential moving average. Returns a list same length as `values`."""
def ema(values: List[float], length: int) -> List[float]:
    k = 2 / (length + 1); e=None; out=[]
    for v in values:
        e = v if e is None else e + k*(v-e); out.append(e)
    return out
"""Relative Strength Index (Wilder). Returns a list same length as `closes` with None until enough points."""
def rsi(closes: List[float], length: int = 14) -> List[Optional[float]]:
    if len(closes) < length + 1: return [None]*len(closes)
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i-1]
        gains.append(max(ch,0.0)); losses.append(max(-ch,0.0))
    avg_g = sum(gains[:length])/length; avg_l = sum(losses[:length])/length
    rsis=[None]*length
    for i in range(length, len(gains)):
        avg_g=(avg_g*(length-1)+gains[i])/length
        avg_l=(avg_l*(length-1)+losses[i])/length
        rs=(avg_g/avg_l) if avg_l!=0 else 100
        rsis.append(100-(100/(1+rs)))
    return [None]+rsis
"""MACD triple: (macd_line, signal, histogram) using EMA(fast/slow/signal)."""
def macd(closes: List[float], fast=12, slow=26, signal_len=9) -> Tuple[float,float,float]:
    ef=ema(closes, fast); es=ema(closes, slow)
    macd_line=[f-s for f,s in zip(ef[-len(es):], es)]
    sig=ema(macd_line, signal_len)
    return macd_line[-1], sig[-1], macd_line[-1]-sig[-1]
"""Volume Weighted Average Price (rolling). Returns a list same length as inputs."""
def vwap(highs: List[float], lows: List[float], closes: List[float], volumes: List[float]) -> List[float]:
    out=[]; cum_pv=0.0; cum_v=0.0
    for h,l,c,v in zip(highs, lows, closes, volumes):
        tp=(h+l+c)/3.0; cum_pv+=tp*v; cum_v+=v; out.append(cum_pv/max(cum_v,1e-9))
    return out
"""Anchored VWAP from start_idx (inclusive). Returns list aligned to inputs with None before start_idx."""
def anchored_vwap(highs,lows,closes,volumes,start_idx:int):
    out=[None]*start_idx; cum_pv=0.0; cum_v=0.0
    for i in range(start_idx, len(closes)):
        tp=(highs[i]+lows[i]+closes[i])/3.0; cum_pv+=tp*volumes[i]; cum_v+=volumes[i]
        out.append(cum_pv/max(cum_v,1e-9))
    return out

# =====================
# Additional core indicators (centralized here to avoid duplication)
# =====================
from typing import Iterable, Union
Number = Union[int, float]


def sma(values: List[Number], length: int) -> List[Optional[float]]:
    """Simple moving average.
    Returns a list the same length as `values`, with `None` until enough points.
    """
    n = int(max(1, length))
    out: List[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += float(v)
        if i >= n:
            s -= float(values[i - n])
        out.append(None if i < n - 1 else s / n)
    return out


def atr(highs: List[Number], lows: List[Number], closes: List[Number], length: int = 14) -> List[Optional[float]]:
    """Average True Range (Wilder).
    Returns a list with `None` for the first `length`-1 elements to align with Wilder smoothing.
    """
    n = int(max(1, length))
    m = min(len(highs), len(lows), len(closes))
    if m == 0:
        return []
    # True Range series
    tr: List[float] = [0.0] * m
    prev_close = float(closes[0])
    for i in range(1, m):
        h = float(highs[i]); l = float(lows[i]); pc = float(prev_close)
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        prev_close = float(closes[i])
    # Wilder smoothing
    out: List[Optional[float]] = [None] * m
    if m <= n:
        return out
    # initial ATR = average of first n TR values starting at index 1 (first TR meaningful)
    init = sum(tr[1:n+1]) / n
    out[n] = init
    atr_prev = init
    for i in range(n + 1, m):
        atr_prev = (atr_prev * (n - 1) + tr[i]) / n
        out[i] = atr_prev
    return out


def adx(highs: List[Number], lows: List[Number], closes: List[Number], length: int = 14) -> List[Optional[float]]:
    """Average Directional Index (Wilder).
    Returns a list aligned to inputs, with `None` until enough points for smoothing.
    """
    n = int(max(1, length))
    m = min(len(highs), len(lows), len(closes))
    if m == 0:
        return []
    # Prepare arrays
    plus_dm = [0.0] * m
    minus_dm = [0.0] * m
    tr = [0.0] * m

    for i in range(1, m):
        up_move = float(highs[i]) - float(highs[i - 1])
        down_move = float(lows[i - 1]) - float(lows[i])
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        h = float(highs[i]); l = float(lows[i]); pc = float(closes[i - 1])
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))

    # Wilder smoothing for TR, +DM, -DM
    def wilder_smooth(arr: List[float]) -> List[Optional[float]]:
        out: List[Optional[float]] = [None] * m
        if m <= n:
            return out
        init = sum(arr[1:n+1])  # sum over first n values (starting at 1)
        out[n] = init
        prev = init
        for i in range(n + 1, m):
            prev = prev - (prev / n) + arr[i]
            out[i] = prev
        return out

    tr_s = wilder_smooth(tr)
    pdm_s = wilder_smooth(plus_dm)
    mdm_s = wilder_smooth(minus_dm)

    # +DI / -DI
    plus_di: List[Optional[float]] = [None] * m
    minus_di: List[Optional[float]] = [None] * m
    for i in range(m):
        if tr_s[i] is None or float(tr_s[i]) == 0.0:
            plus_di[i] = None
            minus_di[i] = None
        else:
            plus_di[i] = 100.0 * float(pdm_s[i]) / float(tr_s[i]) if pdm_s[i] is not None else None
            minus_di[i] = 100.0 * float(mdm_s[i]) / float(tr_s[i]) if mdm_s[i] is not None else None

    # DX and ADX
    dx: List[Optional[float]] = [None] * m
    for i in range(m):
        p = plus_di[i]; m_ = minus_di[i]
        if p is None or m_ is None or (p + m_) == 0:
            dx[i] = None
        else:
            dx[i] = 100.0 * abs((p - m_) / (p + m_))

    # Wilder smoothing of DX to get ADX
    adx_out: List[Optional[float]] = [None] * m
    # collect non-None DX values to seed; DX starts being valid after 2*n bars typically
    valid_dx = [d for d in dx if d is not None]
    if len(valid_dx) < n:
        return adx_out
    # Find first index where we have n consecutive non-None DX values
    start = 0
    consec = 0
    for i in range(m):
        if dx[i] is not None:
            consec += 1
            if consec == n:
                start = i
                break
        else:
            consec = 0
    if consec < n:
        return adx_out
    # initial ADX
    init_adx = sum([d for d in dx[start - n + 1:start + 1] if d is not None]) / n
    adx_out[start] = init_adx
    prev = init_adx
    for i in range(start + 1, m):
        if dx[i] is None:
            adx_out[i] = None
        else:
            prev = (prev * (n - 1) + dx[i]) / n
            adx_out[i] = prev
    return adx_out


# =====================
# Convenience wrappers for RSI (built on top of above Wilder RSI)
# =====================
from typing import cast

def rsi_compact(closes: List[float], length: int = 14) -> List[float]:
    """Return RSI series with None values removed (compact tail-aligned list).
    Useful when downstream consumers only need valid values (e.g., slope checks).
    """
    base = rsi(closes, length)
    return [cast(float, v) for v in base if isinstance(v, (int, float))]


def rsi_last(closes: List[float], length: int = 14) -> Optional[float]:
    """Return the latest available RSI value (or None if insufficient data)."""
    base = rsi(closes, length)
    for v in reversed(base):
        if isinstance(v, (int, float)):
            return float(v)
    return None
