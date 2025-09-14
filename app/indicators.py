from typing import List, Optional, Tuple, Union

"""Core technical indicators with Ruff-friendly style."""

Number = Union[int, float]


def ema(values: List[float], length: int) -> List[float]:
    """Exponential moving average.

    Returns a list the same length as ``values``.
    """
    k = 2 / (length + 1)
    e: Optional[float] = None
    out: List[float] = []
    for v in values:
        e = v if e is None else e + k * (v - e)
        out.append(float(e))
    return out


def rsi(closes: List[float], length: int = 14) -> List[Optional[float]]:
    """Relative Strength Index (Wilder).

    Returns a list the same length as ``closes`` with ``None`` until enough points.
    """
    if len(closes) < length + 1:
        return [None] * len(closes)

    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        ch = float(closes[i]) - float(closes[i - 1])
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))

    avg_g = sum(gains[:length]) / length
    avg_l = sum(losses[:length]) / length

    rsis: List[Optional[float]] = [None] * length
    for i in range(length, len(gains)):
        avg_g = (avg_g * (length - 1) + gains[i]) / length
        avg_l = (avg_l * (length - 1) + losses[i]) / length
        rs = (avg_g / avg_l) if avg_l != 0 else 100.0
        rsis.append(100.0 - (100.0 / (1.0 + rs)))

    head: List[Optional[float]] = [None]
    return head + rsis


def macd(
    closes: List[float],
    fast: int = 12,
    slow: int = 26,
    signal_len: int = 9,
) -> Tuple[float, float, float]:
    """MACD triple: (macd_line, signal, histogram) using EMA(fast/slow/signal)."""
    ef = ema(closes, fast)
    es = ema(closes, slow)
    # Align series to the length of the slow EMA
    macd_line = [f - s for f, s in zip(ef[-len(es) :], es)]
    sig = ema(macd_line, signal_len)
    return macd_line[-1], sig[-1], macd_line[-1] - sig[-1]


def vwap(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
) -> List[float]:
    """Rolling Volume Weighted Average Price.

    Returns a list the same length as the inputs.
    """
    out: List[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for h, lo, c, v in zip(highs, lows, closes, volumes):
        tp = (float(h) + float(lo) + float(c)) / 3.0
        cum_pv += tp * float(v)
        cum_v += float(v)
        out.append(cum_pv / max(cum_v, 1e-9))
    return out


def anchored_vwap(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    volumes: List[float],
    start_idx: int,
) -> List[Optional[float]]:
    """Anchored VWAP from ``start_idx`` (inclusive).

    Returns list aligned to inputs with ``None`` before ``start_idx``.
    """
    n = len(closes)
    if n == 0:
        return []
    start = max(0, int(start_idx))
    out: List[Optional[float]] = [None] * min(start, n)
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(start, n):
        tp = (float(highs[i]) + float(lows[i]) + float(closes[i])) / 3.0
        cum_pv += tp * float(volumes[i])
        cum_v += float(volumes[i])
        out.append(cum_pv / max(cum_v, 1e-9))
    # If start > n, pad to n with None to keep alignment
    if len(out) < n:
        return out
    return out[:n]


# =====================
# Additional core indicators (centralized here to avoid duplication)
# =====================


def sma(values: List[Number], length: int) -> List[Optional[float]]:
    """Simple moving average.

    Returns a list the same length as ``values``, with ``None`` until enough points.
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


def atr(
    highs: List[Number],
    lows: List[Number],
    closes: List[Number],
    length: int = 14,
) -> List[Optional[float]]:
    """Average True Range (Wilder).

    Returns a list with ``None`` for the first ``length-1`` elements (Wilder smoothing).
    """
    n = int(max(1, length))
    m = min(len(highs), len(lows), len(closes))
    if m == 0:
        return []

    tr: List[float] = [0.0] * m
    prev_close = float(closes[0])
    for i in range(1, m):
        h = float(highs[i])
        lo = float(lows[i])
        pc = float(prev_close)
        tr[i] = max(h - lo, abs(h - pc), abs(lo - pc))
        prev_close = float(closes[i])

    out: List[Optional[float]] = [None] * m
    if m <= n:
        return out

    # initial ATR = average of first n TR values starting at index 1
    init = sum(tr[1 : n + 1]) / n
    out[n] = init
    atr_prev = init
    for i in range(n + 1, m):
        atr_prev = (atr_prev * (n - 1) + tr[i]) / n
        out[i] = atr_prev
    return out


def adx(
    highs: List[Number],
    lows: List[Number],
    closes: List[Number],
    length: int = 14,
) -> List[Optional[float]]:
    """Average Directional Index (Wilder).

    Returns a list aligned to inputs, with ``None`` until enough points for smoothing.
    """
    n = int(max(1, length))
    m = min(len(highs), len(lows), len(closes))
    if m == 0:
        return []

    plus_dm = [0.0] * m
    minus_dm = [0.0] * m
    tr = [0.0] * m

    for i in range(1, m):
        up_move = float(highs[i]) - float(highs[i - 1])
        down_move = float(lows[i - 1]) - float(lows[i])
        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0
        h = float(highs[i])
        lo = float(lows[i])
        pc = float(closes[i - 1])
        tr[i] = max(h - lo, abs(h - pc), abs(lo - pc))

    def wilder_smooth(arr: List[float]) -> List[Optional[float]]:
        out_s: List[Optional[float]] = [None] * m
        if m <= n:
            return out_s
        init_s = sum(arr[1 : n + 1])
        out_s[n] = init_s
        prev_s = init_s
        for i in range(n + 1, m):
            prev_s = prev_s - (prev_s / n) + arr[i]
            out_s[i] = prev_s
        return out_s

    tr_s = wilder_smooth(tr)
    pdm_s = wilder_smooth(plus_dm)
    mdm_s = wilder_smooth(minus_dm)

    plus_di: List[Optional[float]] = [None] * m
    minus_di: List[Optional[float]] = [None] * m
    for i in range(m):
        tsi = tr_s[i]
        if tsi is None or tsi == 0.0:
            plus_di[i] = None
            minus_di[i] = None
        else:
            pval = pdm_s[i]
            mval = mdm_s[i]
            plus_di[i] = (100.0 * pval / tsi) if pval is not None else None
            minus_di[i] = (100.0 * mval / tsi) if mval is not None else None

    dx: List[Optional[float]] = [None] * m
    for i in range(m):
        p = plus_di[i]
        m_ = minus_di[i]
        if p is not None and m_ is not None:
            denom = p + m_
            dx[i] = None if denom == 0.0 else 100.0 * abs((p - m_) / denom)
        else:
            dx[i] = None

    adx_out: List[Optional[float]] = [None] * m

    valid_dx: List[float] = [d for d in dx if d is not None]
    if len(valid_dx) < n:
        return adx_out

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

    window: List[float] = [d for d in dx[start - n + 1 : start + 1] if d is not None]
    init_adx = sum(window) / n
    adx_out[start] = init_adx
    prev = init_adx
    for i in range(start + 1, m):
        dxi = dx[i]
        if dxi is None:
            adx_out[i] = None
        else:
            prev = (prev * (n - 1) + dxi) / n
            adx_out[i] = prev
    return adx_out


# =====================
# Convenience wrappers for RSI (built on top of above Wilder RSI)
# =====================


def rsi_compact(closes: List[float], length: int = 14) -> List[float]:
    """Return RSI series with None values removed (tail-aligned list)."""
    base = rsi(closes, length)
    return [float(v) for v in base if isinstance(v, (int, float))]


def rsi_last(closes: List[float], length: int = 14) -> Optional[float]:
    """Return the latest available RSI value (or None if insufficient data)."""
    base = rsi(closes, length)
    for v in reversed(base):
        if isinstance(v, (int, float)):
            return float(v)
    return None
