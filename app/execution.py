# app/execution.py
import json
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, List, Optional, cast

if TYPE_CHECKING:
    from .managers.trendscalp_fsm import Context as PEVContext

import ccxt

from . import config as C
from . import db, telemetry
from .managers.trendscalp_fsm import build_entry_validity_snapshot

# --- helpers ---


def _oid(kind: str) -> str:
    """Generate a stable-looking paper order id."""
    try:
        return f"paper-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}-{kind}"
    except Exception:
        return f"paper-{kind}-{int(time.time())}"


def _round_px(x: float) -> float:
    try:
        return round(float(x), 4)
    except Exception:
        return x


def _get_orders_safe(trade_id: int) -> list[dict]:
    try:
        _get_orders = getattr(db, "get_orders", None)
        return _get_orders(trade_id) if callable(_get_orders) else []
    except Exception:
        return []


def _tp_exists_at(trade_id: int, px: float, tol: float = 0.0005) -> bool:
    orders = _get_orders_safe(trade_id)
    for o in orders or []:
        if (o.get("kind", "").startswith("take_profit")) and (
            o.get("status") in {"open", "filled"}
        ):
            try:
                price_val = o.get("price")
                if price_val is None:
                    continue
                if abs(float(price_val) - float(px)) <= tol:
                    return True
            except Exception:
                continue
    return False


def _remaining_qty(trade_id: int, default_qty: float) -> float:
    """Best-effort remaining position size from DB orders/position."""
    try:
        pos = getattr(db, "get_position", None)
        if callable(pos):
            p = pos(trade_id)
            if p and float(p.get("qty", 0.0)) > 0.0:
                return float(p["qty"])
    except Exception:
        pass
    return float(default_qty)


# Structured TP parser — supports both legacy float TPs and structured {px, size_frac}


def _parse_structured_tps(sig_tps, qty: float):
    try:
        tps = list(sig_tps or [])
    except Exception:
        return [], [], False
    if not tps:
        return [], [], False
    # case: list of dicts with px and size_frac
    if isinstance(tps[0], dict) and ("px" in tps[0]):
        levels = []
        fracs = []
        for it in tps:
            try:
                px = _round_px(float(it.get("px")))
                frac = float(it.get("size_frac", 0.0))
            except Exception:
                continue
            if not (px and frac >= 0.0):
                continue
            levels.append(px)
            fracs.append(frac)
        if not levels:
            return [], [], False
        s = sum(fracs)
        if s > 1.0 + 1e-6:
            fracs = [f / s for f in fracs]
        return levels, fracs, True
    # legacy: list of floats
    levels = []
    for x in tps:
        try:
            levels.append(_round_px(float(x)))
        except Exception:
            pass
    return (levels if levels else []), [], False


def _already_bracketed(trade_id: int) -> bool:
    """Return True if a market_entry was already recorded for this trade_id (best-effort)."""
    try:
        _get_orders = getattr(db, "get_orders", None)
        orders = _get_orders(trade_id) if callable(_get_orders) else []
    except Exception:
        orders = []
    for o in orders or []:
        if (o.get("kind") == "market_entry") and (o.get("status") in {"open", "filled"}):
            return True
    return False


def ensure_partial_tp1(
    ex: ccxt.Exchange,
    symbol: str,
    sig,
    trade_id: int,
    fraction: float,
    qty_hint: Optional[float] = None,
) -> Optional[str]:
    """
    Ensure a reduce-only partial at TP1 exists. Place a limit order for `fraction` of remaining qty.
    Returns created order id (or None if skipped). Safe in DRY_RUN and idempotent by price.
    """
    try:
        tps = list(getattr(sig, "tps") or [])
        if not tps:
            telemetry.log("exec", "PARTIAL_TP1_SKIP", "no TP1 available", {"trade_id": trade_id})
            return None
        tp1 = _round_px(float(tps[0]))
    except Exception:
        telemetry.log("exec", "PARTIAL_TP1_ERR", "cannot parse TP1", {"trade_id": trade_id})
        return None

    fraction = max(0.0, min(1.0, float(fraction)))
    if fraction <= 0.0:
        telemetry.log("exec", "PARTIAL_TP1_SKIP", "fraction <= 0", {"trade_id": trade_id})
        return None

    if _tp_exists_at(trade_id, tp1):
        telemetry.log(
            "exec", "PARTIAL_TP1_EXISTS", "tp1 already present", {"trade_id": trade_id, "px": tp1}
        )
        return None

    # Determine remaining qty
    rem_qty = _remaining_qty(trade_id, float(qty_hint or 0.0))
    tp_qty = max(0.0, round(rem_qty * fraction, 8))
    if tp_qty <= 0.0:
        telemetry.log("exec", "PARTIAL_TP1_SKIP", "computed qty <= 0", {"trade_id": trade_id})
        return None

    side_exit = "sell" if getattr(sig, "side", "LONG") == "LONG" else "buy"

    if C.DRY_RUN:
        oid = _oid("tp1_partial")
        db.add_order(trade_id, oid, "take_profit_1", side_exit, tp1, tp_qty, "open")
        telemetry.log(
            "exec",
            "PARTIAL_TP1_PAPER",
            "paper partial TP1 created",
            {"trade_id": trade_id, "px": tp1, "qty": tp_qty},
        )
        return oid

    try:
        tp_order = ex.create_order(
            symbol,
            type="limit",
            side=side_exit,
            amount=tp_qty,
            price=tp1,
            params={"reduceOnly": True},
        )
        oid = tp_order.get("id", "")
        db.add_order(trade_id, oid, "take_profit_1", side_exit, tp1, tp_qty, "open")
        telemetry.log(
            "exec",
            "PARTIAL_TP1_LIVE",
            "live partial TP1 placed",
            {"trade_id": trade_id, "px": tp1, "qty": tp_qty},
        )
        return oid
    except Exception as e:
        telemetry.log("exec", "PARTIAL_TP1_ERROR", str(e), {"trade_id": trade_id, "px": tp1})
        return None


def exit_remainder_market(
    ex: ccxt.Exchange,
    symbol: str,
    sig,
    trade_id: int,
    qty_hint: Optional[float] = None,
) -> Optional[str]:
    """Flatten any remaining position at market (used on RUNNER->CHOP flip before TP2)."""
    side_exit = "sell" if getattr(sig, "side", "LONG") == "LONG" else "buy"
    rem_qty = _remaining_qty(trade_id, float(qty_hint or 0.0))
    if rem_qty <= 0.0:
        telemetry.log("exec", "EXIT_REMAINDER_SKIP", "no remaining qty", {"trade_id": trade_id})
        return None

    if C.DRY_RUN:
        oid = _oid("flatten")
        db.add_order(trade_id, oid, "market_exit", side_exit, 0.0, rem_qty, "filled")
        telemetry.log(
            "exec",
            "EXIT_REMAINDER_PAPER",
            "paper market exit",
            {"trade_id": trade_id, "qty": rem_qty},
        )
        # --- DRY_RUN bookkeeping: mark position closed and cancel open protective orders ---
        try:
            # Close paper position (preferred)
            close_pos = getattr(db, "close_position", None)
            if callable(close_pos):
                close_pos(trade_id)
            else:
                # Fallback: zero out qty if supported
                set_qty = getattr(db, "set_position_qty", None)
                if callable(set_qty):
                    set_qty(trade_id, 0.0)
            telemetry.log(
                "exec", "PAPER_POS_CLOSED", "position closed (dry)", {"trade_id": trade_id}
            )
        except Exception:
            pass
        try:
            # Best-effort cancel any open SL/TP orders so surveillance won't continue to manage them
            upd = getattr(db, "update_order_status", None)
            canc_all = getattr(db, "cancel_open_orders", None)
            if callable(upd):
                for o in _get_orders_safe(trade_id):
                    if (
                        (o or {}).get("status") == "open"
                        and str((o or {}).get("kind", "")).startswith(("stop_loss"))
                        or str((o or {}).get("kind", "")).startswith("take_profit")
                    ):
                        try:
                            upd(trade_id, o.get("id"), "canceled")
                        except Exception:
                            continue
            elif callable(canc_all):
                try:
                    canc_all(trade_id)
                except Exception:
                    pass
        except Exception:
            pass
        return oid

    try:
        order = ex.create_order(symbol, type="market", side=side_exit, amount=rem_qty)
        oid = order.get("id", "")
        db.add_order(
            trade_id,
            oid,
            "market_exit",
            side_exit,
            float(order.get("average") or 0.0),
            rem_qty,
            "filled",
        )
        telemetry.log(
            "exec",
            "EXIT_REMAINDER_LIVE",
            "live market exit",
            {"trade_id": trade_id, "qty": rem_qty},
        )
        return oid
    except Exception as e:
        telemetry.log("exec", "EXIT_REMAINDER_ERROR", str(e), {"trade_id": trade_id})
        return None


def place_bracket(ex: ccxt.Exchange, symbol: str, sig, qty: float, trade_id: int):
    """
    Entry + initial SL and (optionally) a single full-size TP3 (reduceOnly).
    No TP1/TP2 orders are placed; surveillance moves SL at TP1/TP2 and we hold full size.
    """
    # --- Extract and validate prices from signal
    try:
        entry_px = float(getattr(sig, "entry"))
        sl_px = float(getattr(sig, "sl"))
    except Exception as e:
        telemetry.log("exec", "SIG_FIELDS_ERROR", f"bad entry/SL on signal: {e}", {})
        raise

    # TP3 is optional; tolerate None / empty list
    tp3_px = None
    try:
        _tps = list(getattr(sig, "tps") or [])
        if _tps:
            _last = _tps[-1]
            if _last is not None:
                tp3_px = float(_last)
    except Exception:
        tp3_px = None

    # Parse structured TPs (if provided by tp_calc with TS_TP_STRUCTURED=true)
    tp_levels, tp_fracs, is_structured = _parse_structured_tps(getattr(sig, "tps", []), qty)

    side_entry = "buy" if sig.side == "LONG" else "sell"
    side_exit = "sell" if sig.side == "LONG" else "buy"

    entry_px = _round_px(entry_px)
    sl_px = _round_px(sl_px)
    if tp3_px is not None:
        tp3_px = _round_px(tp3_px)

    # --- Validate quantity
    try:
        qty = float(qty)
    except Exception as e:
        telemetry.log("exec", "QTY_ERROR", f"qty parse failed: {e}", {"qty": qty})
        raise
    if qty <= 0:
        telemetry.log(
            "exec",
            "QTY_ERROR_NONPOS",
            "non‑positive qty for place_bracket",
            {"qty": qty},
        )
        raise ValueError("quantity must be > 0")

    # Engine/exchange metadata (non-breaking)
    try:
        _meta = getattr(sig, "meta", {}) or {}
    except Exception:
        _meta = {}
    engine = _meta.get("engine") or "trendscalp"
    exch_id = _meta.get("exchange", getattr(ex, "id", "delta"))
    sym_name = symbol

    # Build entry-validity snapshot at fill time if features are available in meta
    try:
        feats5 = _meta.get("feats_5m") or _meta.get("feats") or {}
        if isinstance(feats5, dict) and feats5:
            ctx0 = SimpleNamespace(
                is_long=(sig.side == "LONG"),
                price=entry_px,
                meta={"ts": time.time()},
            )
            _meta["entry_validity"] = build_entry_validity_snapshot(
                cast("PEVContext", ctx0), feats5
            )
            try:
                telemetry.log(
                    "exec",
                    "PEV_SNAPSHOT",
                    "entry validity stored",
                    {
                        "trade_id": trade_id,
                        "engine": engine,
                        "side": sig.side,
                        "entry": entry_px,
                        "snapshot": _meta["entry_validity"],
                    },
                )
            except Exception:
                pass
    except Exception:
        pass

    preplace_partial = bool(
        _meta.get("preplace_tp1_partial", getattr(C, "PREPLACE_TP1_PARTIAL", False))
    )
    partial_frac = float(_meta.get("partial_frac", getattr(C, "TS_PARTIAL_TP1", 0.5)))

    # Idempotency: if this trade_id already has a market_entry, do not place another bracket
    if _already_bracketed(trade_id):
        telemetry.log(
            "exec",
            "BRACKET_EXISTS",
            "skipping duplicate bracket for trade",
            {
                "trade_id": trade_id,
                "engine": engine,
            },
        )
        return []

    print(
        f"[EXEC] [{engine}] {sig.side} {qty} {symbol} @ {entry_px} | "
        f"SL {sl_px} | TP3 {tp3_px} | EXCH {exch_id}"
    )
    order_ids = []

    if C.DRY_RUN:
        # ---- Simulated orders recorded in DB so surveillance can manage them ----
        eid = _oid("entry")
        db.add_order(trade_id, eid, "market_entry", side_entry, entry_px, qty, "filled")
        order_ids.append(eid)

        sid = _oid("sl")
        db.add_order(trade_id, sid, "stop_loss", side_exit, sl_px, qty, "open")
        order_ids.append(sid)

        if preplace_partial and not is_structured:
            try:
                tps = list(getattr(sig, "tps") or [])
                if tps:
                    tp1_px = _round_px(float(tps[0]))
                    tp_qty = max(0.0, round(qty * float(max(0.0, min(1.0, partial_frac))), 8))
                    if tp_qty > 0.0 and not _tp_exists_at(trade_id, tp1_px):
                        tid = _oid("tp1_partial")
                        db.add_order(
                            trade_id, tid, "take_profit_1", side_exit, tp1_px, tp_qty, "open"
                        )
                        order_ids.append(tid)
                        telemetry.log(
                            "exec",
                            "PREPLACE_TP1_PAPER",
                            "paper partial TP1 pre-placed",
                            {"trade_id": trade_id, "px": tp1_px, "qty": tp_qty},
                        )
            except Exception:
                pass

        if is_structured:
            tp_summ = []
            for idx, (px, frac) in enumerate(zip(tp_levels, tp_fracs), start=1):
                tp_qty = max(0.0, round(qty * float(frac), 8))
                if tp_qty <= 0:
                    continue
                tid = _oid(f"tp{idx}")
                db.add_order(
                    trade_id,
                    tid,
                    f"take_profit_{idx}",
                    side_exit,
                    _round_px(px),
                    tp_qty,
                    "open",
                )
                order_ids.append(tid)
                tp_summ.append(
                    {
                        "idx": idx,
                        "px": _round_px(px),
                        "frac": float(frac),
                        "qty": tp_qty,
                    }
                )
            telemetry.log(
                "exec",
                "TP_SPLIT_PLACED",
                "paper split-TPs created",
                {"trade_id": trade_id, "tps": tp_summ},
            )
        else:
            if C.PLACE_TP3_LIMIT and tp3_px is not None:
                tid = _oid("tp3")
                db.add_order(trade_id, tid, "take_profit_final", side_exit, tp3_px, qty, "open")
                order_ids.append(tid)
            elif C.PLACE_TP3_LIMIT and tp3_px is None:
                telemetry.log(
                    "exec",
                    "TP3_SKIPPED",
                    "no tp3 in signal; skipping TP limit",
                    {"trade_id": trade_id},
                )

        telemetry.log(
            "exec",
            "PAPER_ORDERS",
            f"{sig.side} paper orders created",
            {
                "trade_id": trade_id,
                "entry": entry_px,
                "sl": sl_px,
                "tp3": tp3_px,
                "qty": qty,
                "order_ids": order_ids,
                "engine": engine,
                "exchange": exch_id,
                "symbol": sym_name,
            },
        )
        # Open a paper position so manage loop reconciles qty>0
        try:
            _set_position = getattr(db, "set_position", None)
            if callable(_set_position):
                _set_position(
                    trade_id,
                    sig.side,
                    entry_px,
                    qty,
                    status="open",
                    meta={
                        "engine": engine,
                        "exchange": exch_id,
                        "symbol": sym_name,
                        **(
                            {"entry_validity": _meta.get("entry_validity")}
                            if _meta.get("entry_validity")
                            else {}
                        ),
                    },
                )
            else:
                raise AttributeError("set_position not implemented")
        except Exception:
            # If your DB layer uses a different API, we still keep orders for manage to infer state
            try:
                db.append_event(trade_id, "PAPER_POS_OPEN", f"{sig.side} {qty} @ {entry_px}")
            except Exception:
                pass

        print("[DRY_RUN] No live orders sent.")
        return order_ids

    # ---- LIVE mode via exchange ----
    try:
        entry_order = ex.create_order(symbol, type="market", side=side_entry, amount=qty)
        oid = entry_order.get("id", "")
        order_ids.append(oid)
        filled_px = entry_order.get("average") or entry_order.get("price") or entry_px
        filled_px = _round_px(filled_px)
        db.add_order(trade_id, oid, "market_entry", side_entry, filled_px, qty, "filled")
    except Exception as e:
        telemetry.log(
            "exec",
            "ENTRY_ERROR",
            str(e),
            {
                "symbol": sym_name,
                "engine": engine,
                "exchange": exch_id,
            },
        )
        raise

    try:
        params_sl = {"reduceOnly": True, "triggerPrice": sl_px}
        sl_order = ex.create_order(
            symbol,
            type="stop",
            side=side_exit,
            amount=qty,
            price=None,
            params=params_sl,
        )
        oid = sl_order.get("id", "")
        order_ids.append(oid)
        db.add_order(trade_id, oid, "stop_loss", side_exit, sl_px, qty, "open")
    except Exception as e:
        try:
            db.append_event(trade_id, "SL_ERROR", f"SL failed: {e}")
        except Exception:
            pass
        telemetry.log(
            "exec",
            "SL_ERROR",
            str(e),
            {
                "symbol": sym_name,
                "engine": engine,
                "exchange": exch_id,
            },
        )

    if preplace_partial and not is_structured:
        try:
            tps = list(getattr(sig, "tps") or [])
            if tps:
                tp1_px = _round_px(float(tps[0]))
                tp_qty = max(0.0, round(qty * float(max(0.0, min(1.0, partial_frac))), 8))
                if tp_qty > 0.0 and not _tp_exists_at(trade_id, tp1_px):
                    tp_order = ex.create_order(
                        symbol,
                        type="limit",
                        side=side_exit,
                        amount=tp_qty,
                        price=tp1_px,
                        params={"reduceOnly": True},
                    )
                    oid = tp_order.get("id", "")
                    order_ids.append(oid)
                    db.add_order(trade_id, oid, "take_profit_1", side_exit, tp1_px, tp_qty, "open")
                    telemetry.log(
                        "exec",
                        "PREPLACE_TP1_LIVE",
                        "live partial TP1 pre-placed",
                        {"trade_id": trade_id, "px": tp1_px, "qty": tp_qty},
                    )
        except Exception as e:
            telemetry.log("exec", "PREPLACE_TP1_ERROR", str(e), {"trade_id": trade_id})

    if is_structured:
        tp_summ = []
        for idx, (px, frac) in enumerate(zip(tp_levels, tp_fracs), start=1):
            tp_qty = max(0.0, round(qty * float(frac), 8))
            if tp_qty <= 0:
                continue
            try:
                tp_order = ex.create_order(
                    symbol,
                    type="limit",
                    side=side_exit,
                    amount=tp_qty,
                    price=_round_px(px),
                    params={"reduceOnly": True},
                )
                oid = tp_order.get("id", "")
                order_ids.append(oid)
                db.add_order(
                    trade_id,
                    oid,
                    f"take_profit_{idx}",
                    side_exit,
                    _round_px(px),
                    tp_qty,
                    "open",
                )
                tp_summ.append(
                    {
                        "idx": idx,
                        "px": _round_px(px),
                        "frac": float(frac),
                        "qty": tp_qty,
                    }
                )
            except Exception as e:
                try:
                    db.append_event(trade_id, "TP_ERROR", f"TP{idx} failed: {e}")
                except Exception:
                    pass
                telemetry.log(
                    "exec",
                    "TP_ERROR",
                    str(e),
                    {
                        "symbol": sym_name,
                        "engine": engine,
                        "exchange": exch_id,
                        "tp_idx": idx,
                    },
                )
        telemetry.log(
            "exec",
            "TP_SPLIT_PLACED",
            "live split-TPs placed",
            {"trade_id": trade_id, "tps": tp_summ},
        )
    elif C.PLACE_TP3_LIMIT and tp3_px is not None:
        try:
            tp_order = ex.create_order(
                symbol,
                type="limit",
                side=side_exit,
                amount=qty,
                price=tp3_px,
                params={"reduceOnly": True},
            )
            oid = tp_order.get("id", "")
            order_ids.append(oid)
            db.add_order(trade_id, oid, "take_profit_final", side_exit, tp3_px, qty, "open")
        except Exception as e:
            try:
                db.append_event(trade_id, "TP3_ERROR", f"TP3 failed: {e}")
            except Exception:
                pass
            telemetry.log(
                "exec",
                "TP3_ERROR",
                str(e),
                {
                    "symbol": sym_name,
                    "engine": engine,
                    "exchange": exch_id,
                },
            )

    telemetry.log(
        "exec",
        "LIVE_ORDERS",
        f"{sig.side} live orders placed",
        {
            "trade_id": trade_id,
            "entry": entry_px,
            "sl": sl_px,
            "tp3": tp3_px,
            "qty": qty,
            "order_ids": order_ids,
            "engine": engine,
            "exchange": exch_id,
            "symbol": sym_name,
        },
    )
    # Persist entry_validity into LIVE position meta (best-effort)
    try:
        if _meta.get("entry_validity"):
            set_pos_meta = getattr(db, "set_position_meta", None)
            if callable(set_pos_meta):
                set_pos_meta(trade_id, {"entry_validity": _meta["entry_validity"]})
            else:
                # Fallback: append an event so we can recover snapshot later from telemetry/DB
                try:
                    db.append_event(
                        trade_id,
                        "PEV_SNAPSHOT",
                        json.dumps(_meta["entry_validity"])[:1000],
                    )
                except Exception:
                    pass
    except Exception:
        pass
    return order_ids


# --- Recovery re-entry: reduced-size bracket after PEV exit ---
def reenter_from_recovery(
    ex: ccxt.Exchange,
    symbol: str,
    sig,
    trade_id: int,
    qty_hint: Optional[float] = None,
    *,
    size_factor: float = 0.6,
) -> List[str] | None:
    """Place a reduced-size bracket as a recovery re-entry after a PEV exit.
    - Uses a fresh signal `sig` (with entry/sl/tps).
    - Quantity = max(0, round(qty_hint * size_factor, 8))
    - Stores an entry-validity snapshot if features are present in `sig.meta`.
    - Emits telemetry for audit: PEV_REENTER_{PAPER|LIVE}.
    - Returns created order ids (or None if skipped/error).
    """
    try:
        # Validate qty
        base = float(qty_hint or 0.0)
        qty = max(0.0, round(base * float(size_factor), 8))
        if qty <= 0.0:
            telemetry.log(
                "exec",
                "PEV_REENTER_SKIP",
                "computed qty <= 0",
                {"trade_id": trade_id, "qty_hint": base, "factor": size_factor},
            )
            return None

        # Avoid double-bracketing (should be a clean slate post-exit)
        if _already_bracketed(trade_id):
            telemetry.log(
                "exec",
                "PEV_REENTER_SKIP",
                "bracket already present",
                {"trade_id": trade_id},
            )
            return None

        # Build a minimal meta from signal
        try:
            _meta = getattr(sig, "meta", {}) or {}
        except Exception:
            _meta = {}
        engine = _meta.get("engine") or "trendscalp"

        # Best-effort entry-validity snapshot (at re-entry time)
        try:
            feats5 = _meta.get("feats_5m") or _meta.get("feats") or {}
            if isinstance(feats5, dict) and feats5:
                ctx0 = SimpleNamespace(
                    is_long=(sig.side == "LONG"),
                    price=float(getattr(sig, "entry")),
                    meta={"ts": time.time()},
                )
                _meta["entry_validity"] = build_entry_validity_snapshot(
                    cast("PEVContext", ctx0), feats5
                )
                telemetry.log(
                    "exec",
                    "PEV_REENTER_SNAPSHOT",
                    "entry validity stored (recovery)",
                    {
                        "trade_id": trade_id,
                        "engine": engine,
                        "side": sig.side,
                        "snapshot": _meta["entry_validity"],
                    },
                )
        except Exception:
            pass

        # Place the reduced-size bracket using the same machinery
        oids = place_bracket(ex, symbol, sig, qty, trade_id)

        if not oids:
            telemetry.log(
                "exec",
                "PEV_REENTER_EMPTY",
                "no orders returned from place_bracket",
                {"trade_id": trade_id, "engine": engine},
            )
            return None

        telemetry.log(
            "exec",
            ("PEV_REENTER_PAPER" if C.DRY_RUN else "PEV_REENTER_LIVE"),
            "recovery re-entry placed",
            {"trade_id": trade_id, "engine": engine, "factor": size_factor, "order_ids": oids},
        )
        return oids
    except Exception as e:
        telemetry.log("exec", "PEV_REENTER_ERROR", str(e), {"trade_id": trade_id})
        return None
