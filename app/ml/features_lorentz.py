from __future__ import annotations

from typing import Dict, List


def build_features(tf5: Dict[str, List[float]]) -> Dict[str, List[float]]:
    closes = tf5.get("close", [])
    highs = tf5.get("high", [])
    lows = tf5.get("low", [])

    # Minimal placeholder; real version will use pandas-ta
    def ema(arr: List[float], n: int) -> List[float]:
        if not arr:
            return []
        n = max(1, int(n))
        k = 2.0 / (n + 1.0)
        out = [float(arr[0])]
        for i in range(1, len(arr)):
            out.append(out[-1] + k * (float(arr[i]) - out[-1]))
        return out

    def rsi(closes: List[float], n: int) -> List[float]:
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

    def atr(highs: List[float], lows: List[float], closes: List[float], n: int) -> List[float]:
        n = max(1, int(n))
        if not highs or not lows or not closes:
            return []
        tr: List[float] = []
        for i in range(len(closes)):
            if i == 0:
                tr.append(float(highs[i]) - float(lows[i]))
            else:
                h = float(highs[i])
                low = float(lows[i])
                pc = float(closes[i - 1])
                tr.append(max(h - low, abs(h - pc), abs(low - pc)))
        # Wilder's smoothing (EMA with alpha = 1/n)
        out = []
        alpha = 1.0 / float(n)
        for i, v in enumerate(tr):
            if i == 0:
                out.append(float(v))
            else:
                out.append(out[-1] + alpha * (float(v) - out[-1]))
        return out

    return {
        "EMA8": ema(closes, 8),
        "EMA20": ema(closes, 20),
        "RSI14": rsi(closes, 14),
        "ATR14": atr(highs, lows, closes, 14),
    }
