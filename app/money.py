# app/money.py
from __future__ import annotations

import os
from typing import Tuple

from . import config as C

# -----------------------
# Config / params
# -----------------------
# Fee rate per side on notional (e.g., 0.05% = 0.0005). Round trip ~ 0.0010.
FEE_RATE_PER_SIDE = float(os.getenv("FEE_RATE_PER_SIDE", "0.0005"))

# Optional hard cap on quantity (lots/contracts)
MAX_QTY_CAP = float(os.getenv("MAX_QTY", "1500"))

# MIN SL distance guards. Prefer MIN_SL_FRAC for clarity (e.g., 0.005 == 0.5%).
# If only MIN_SL_PCT is set, it is interpreted as percent (e.g., 0.5 == 0.5%).
# Minimum SL distance as % of entry (e.g., 0.05% → 0.0005 * entry).
MIN_SL_PCT = float(os.getenv("MIN_SL_PCT", "0.05"))
# Absolute minimum SL distance in quote units.
MIN_SL_ABS = float(os.getenv("MIN_SL_ABS", "0.0"))
# Floor lot size to avoid zero-qty.
MIN_QTY = float(os.getenv("MIN_QTY", "1"))

# Optional minimum notional per order (quote currency). 0 disables.
NOTIONAL_MIN = float(os.getenv("NOTIONAL_MIN", "0.0"))


# Safety: avoid zero/negatives
def _f(x, d=0.0):
    try:
        v = float(x)
        if v != v:  # NaN check
            return d
        return v
    except Exception:
        return d


# Interpret SL minimum either via percent (PCT) or raw fraction (FRAC)
def _min_sl_fraction() -> float:
    """Return a *fraction of entry* to use as the minimum SL distance.
    Priority:
      MIN_SL_FRAC (e.g., 0.005 for 0.5%) > MIN_SL_PCT/100 (e.g., 0.5 -> 0.005).
    """
    try:
        frac = _f(os.getenv("MIN_SL_FRAC", None), -1.0)
        if frac is not None and frac > 0:
            return float(frac)
    except Exception:
        pass
    # fallback to percent semantics (keeps backward-compat with existing configs)
    pct = _f(os.getenv("MIN_SL_PCT", None), -1.0)
    if pct is not None and pct > 0:
        return float(pct) / 100.0
    # final fallback to the module default constant
    return float(MIN_SL_PCT) / 100.0


# -----------------------
# Fees & PnL
# -----------------------
def calc_fees(entry: float, exit_px: float, qty: float) -> float:
    """
    Return fees as a NEGATIVE number for a round-trip:
      fees = -(entry*qty*fee_rate + exit*qty*fee_rate)
    """
    e = _f(entry)
    x = _f(exit_px)
    q = _f(qty)
    if e <= 0 or x <= 0 or q <= 0:
        return 0.0
    fees = -(e * q * FEE_RATE_PER_SIDE + x * q * FEE_RATE_PER_SIDE)
    return float(fees)


def calc_pnl(side: str, entry: float, exit_px: float, qty: float) -> float:
    """
    Gross PnL (no fees).
    LONG:  (exit - entry) * qty
    SHORT: (entry - exit) * qty
    """
    s = (side or "").upper()
    e = _f(entry)
    x = _f(exit_px)
    q = _f(qty)
    if e <= 0 or q <= 0:
        return 0.0
    if s == "LONG":
        return (x - e) * q
    else:
        return (e - x) * q


def calc_pnl_net(side: str, entry: float, exit_px: float, qty: float) -> float:
    """
    Net PnL = gross + fees (fees is already negative)
    """
    g = calc_pnl(side, entry, exit_px, qty)
    f = calc_fees(entry, exit_px, qty)
    return g + f


# -----------------------
# Position sizing
# -----------------------
def _qty_capital(balance_quote: float, entry: float) -> float:
    """
    Capital-fraction sizing with leverage cap:
      notional_allowed = balance * CAPITAL_FRACTION * MAX_LEVERAGE
      qty = notional_allowed / entry
    """
    bal = _f(balance_quote)
    e = _f(entry)
    if bal <= 0 or e <= 0:
        return 0.0
    notional_allowed = (
        bal * max(_f(C.CAPITAL_FRACTION, 0.0), 0.0) * max(_f(C.MAX_LEVERAGE, 1.0), 1.0)
    )
    return max(0.0, notional_allowed / e)


def _qty_risk(balance_quote: float, entry: float, sl: float) -> float:
    """
    Risk-R sizing:
      risk_amount = balance * (RISK_PCT/100)
      per_unit_loss = |entry - sl|
      qty = risk_amount / per_unit_loss
    """
    bal = _f(balance_quote)
    e = _f(entry)
    s = _f(sl)
    if bal <= 0 or e <= 0 or s <= 0:
        return 0.0
    # Guard against zero/too-close SL by enforcing a minimum distance
    raw_loss = abs(e - s)
    # Use either MIN_SL_FRAC (raw) or MIN_SL_PCT/100
    min_by_pct = e * max(_min_sl_fraction(), 0.0)
    min_by_abs = max(_f(MIN_SL_ABS, 0.0), 0.0)
    per_unit_loss = max(raw_loss, min_by_pct, min_by_abs)
    if per_unit_loss <= 0:
        return 0.0
    risk_amount = bal * (max(_f(C.RISK_PCT, 0.0), 0.0) / 100.0)
    return max(0.0, risk_amount / per_unit_loss)


def _apply_qty_caps(qty: float) -> float:
    q = max(0.0, _f(qty))
    if MAX_QTY_CAP > 0:
        q = min(q, MAX_QTY_CAP)
    # If q is positive but tiny, bump to MIN_QTY to avoid 0-qty rejections
    if q > 0 and q < max(_f(MIN_QTY, 0.0), 0.0):
        q = max(_f(MIN_QTY, 0.0), 0.0)
    return q


def choose_size(balance_quote: float, entry: float, sl: float) -> float:
    """
    Combines sizing modes based on C.SIZING_MODE:
      - 'capital_frac' -> use capital fraction only
      - 'risk_r'       -> use risk-R only
      - 'both'         -> min(capital_frac, risk_r)
    Applies MAX_QTY cap and guards.
    """
    # When paper trading, ignore current (possibly negative) equity for sizing.
    effective_balance = balance_quote
    try:
        import app.config as C

        if C.DRY_RUN and getattr(C, "PAPER_USE_START_BALANCE", False):
            effective_balance = float(
                getattr(C, "PAPER_START_BALANCE", balance_quote) or balance_quote
            )
    except Exception:
        pass
    mode = (getattr(C, "SIZING_MODE", "capital_frac") or "capital_frac").strip().lower()
    qc = _qty_capital(effective_balance, entry)
    qr = _qty_risk(effective_balance, entry, sl)

    if mode == "capital_frac":
        q = qc
    elif mode == "risk_r":
        q = qr
    else:
        q = min(qc, qr) if qc > 0 and qr > 0 else max(qc, qr)

    # Final guard: if sizing mode selected a tiny positive value, snap to at least MIN_QTY
    # but never exceed capital-based allowance
    min_q = max(_f(MIN_QTY, 0.0), 0.0)
    if q > 0 and q < min_q:
        # Capital cap derived from current balance & leverage
        cap_q = qc if qc > 0 else _qty_capital(balance_quote, entry)
        if cap_q > 0:
            q = min(min_q, cap_q)

    # Optional exchange notional floor: if enabled and notional too small, return 0 to force skip
    if _f(NOTIONAL_MIN, 0.0) > 0.0 and entry > 0 and q > 0 and (entry * q) < NOTIONAL_MIN:
        return 0.0

    return _apply_qty_caps(q)


# -----------------------
# Convenience: split net/gross in one call (optional)
# -----------------------
def summarize_trade(
    side: str,
    entry: float,
    exit_px: float,
    qty: float,
) -> Tuple[float, float, float]:
    """
    Returns (gross, fees, net)
    """
    g = calc_pnl(side, entry, exit_px, qty)
    f = calc_fees(entry, exit_px, qty)
    n = g + f
    return (g, f, n)
