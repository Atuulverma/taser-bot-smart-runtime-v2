# app/execution.py
import time, uuid
import ccxt
from . import config as C, db, telemetry


# --- helpers ---

def _oid(kind: str) -> str:
    """Generate a stable-looking paper order id."""
    try:
        return f"paper-{int(time.time()*1000)}-{uuid.uuid4().hex[:6]}-{kind}"
    except Exception:
        return f"paper-{kind}-{int(time.time())}"

def _round_px(x: float) -> float:
    try:
        return round(float(x), 4)
    except Exception:
        return x

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
        orders = db.get_orders(trade_id)  # expected to return list[dict] or []
    except Exception:
        orders = []
    for o in orders or []:
        if (o.get("kind") == "market_entry") and (o.get("status") in {"open", "filled"}):
            return True
    return False

def place_bracket(ex: ccxt.Exchange, symbol: str, sig, qty: float, trade_id: int):
    """
    Entry + initial SL and (optionally) a single full-size TP3 (reduceOnly).
    No TP1/TP2 orders are placed; surveillance moves SL at TP1/TP2 and we hold full size.
    """
    # --- Extract and validate prices from signal
    try:
        entry_px = float(getattr(sig, 'entry'))
        sl_px = float(getattr(sig, 'sl'))
    except Exception as e:
        telemetry.log("exec", "SIG_FIELDS_ERROR", f"bad entry/SL on signal: {e}", {})
        raise

    # TP3 is optional; tolerate None / empty list
    tp3_px = None
    try:
        _tps = list(getattr(sig, 'tps') or [])
        if _tps:
            _last = _tps[-1]
            if _last is not None:
                tp3_px = float(_last)
    except Exception:
        tp3_px = None

    # Parse structured TPs (if provided by tp_calc with TS_TP_STRUCTURED=true)
    tp_levels, tp_fracs, is_structured = _parse_structured_tps(getattr(sig, 'tps', []), qty)

    side_entry = "buy" if sig.side == "LONG" else "sell"
    side_exit  = "sell" if sig.side == "LONG" else "buy"

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
        telemetry.log("exec", "QTY_ERROR_NONPOS", "non‑positive qty for place_bracket", {"qty": qty})
        raise ValueError("quantity must be > 0")

    # Engine/exchange metadata (non-breaking)
    try:
        _meta = getattr(sig, "meta", {}) or {}
    except Exception:
        _meta = {}
    engine   = _meta.get("engine") or "trendscalp"
    exch_id  = _meta.get("exchange", getattr(ex, "id", "delta"))
    sym_name = symbol

    # Idempotency: if this trade_id already has a market_entry, do not place another bracket
    if _already_bracketed(trade_id):
        telemetry.log("exec", "BRACKET_EXISTS", "skipping duplicate bracket for trade", {"trade_id": trade_id, "engine": engine})
        return []

    print(f"[EXEC] [{engine}] {sig.side} {qty} {symbol} @ {entry_px} | SL {sl_px} | TP3 {tp3_px} | EXCH {exch_id}")
    order_ids = []

    if C.DRY_RUN:
        # ---- Simulated orders recorded in DB so surveillance can manage them ----
        eid = _oid("entry")
        db.add_order(trade_id, eid, "market_entry", side_entry, entry_px, qty, "filled")
        order_ids.append(eid)

        sid = _oid("sl")
        db.add_order(trade_id, sid, "stop_loss", side_exit, sl_px, qty, "open")
        order_ids.append(sid)

        if is_structured:
            tp_summ = []
            for idx, (px, frac) in enumerate(zip(tp_levels, tp_fracs), start=1):
                tp_qty = max(0.0, round(qty * float(frac), 8))
                if tp_qty <= 0:
                    continue
                tid = _oid(f"tp{idx}")
                db.add_order(trade_id, tid, f"take_profit_{idx}", side_exit, _round_px(px), tp_qty, "open")
                order_ids.append(tid)
                tp_summ.append({"idx": idx, "px": _round_px(px), "frac": float(frac), "qty": tp_qty})
            telemetry.log("exec", "TP_SPLIT_PLACED", "paper split-TPs created", {"trade_id": trade_id, "tps": tp_summ})
        else:
            if C.PLACE_TP3_LIMIT and tp3_px is not None:
                tid = _oid("tp3")
                db.add_order(trade_id, tid, "take_profit_final", side_exit, tp3_px, qty, "open")
                order_ids.append(tid)
            elif C.PLACE_TP3_LIMIT and tp3_px is None:
                telemetry.log("exec", "TP3_SKIPPED", "no tp3 in signal; skipping TP limit", {"trade_id": trade_id})

        telemetry.log("exec", "PAPER_ORDERS",
                      f"{sig.side} paper orders created",
                      {"trade_id": trade_id, "entry": entry_px, "sl": sl_px, "tp3": tp3_px, "qty": qty,
                       "order_ids": order_ids, "engine": engine, "exchange": exch_id, "symbol": sym_name})
        # Open a paper position so manage loop reconciles qty>0
        try:
            db.set_position(trade_id, sig.side, entry_px, qty, status="open", meta={"engine": engine, "exchange": exch_id, "symbol": sym_name})
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
        telemetry.log("exec", "ENTRY_ERROR", str(e), {"symbol": sym_name, "engine": engine, "exchange": exch_id})
        raise

    try:
        params_sl = {"reduceOnly": True, "triggerPrice": sl_px}
        sl_order = ex.create_order(symbol, type="stop", side=side_exit, amount=qty, price=None, params=params_sl)
        oid = sl_order.get("id", "")
        order_ids.append(oid)
        db.add_order(trade_id, oid, "stop_loss", side_exit, sl_px, qty, "open")
    except Exception as e:
        try:
            db.append_event(trade_id, "SL_ERROR", f"SL failed: {e}")
        except Exception:
            pass
        telemetry.log("exec", "SL_ERROR", str(e), {"symbol": sym_name, "engine": engine, "exchange": exch_id})

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
                    params={"reduceOnly": True}
                )
                oid = tp_order.get("id", "")
                order_ids.append(oid)
                db.add_order(trade_id, oid, f"take_profit_{idx}", side_exit, _round_px(px), tp_qty, "open")
                tp_summ.append({"idx": idx, "px": _round_px(px), "frac": float(frac), "qty": tp_qty})
            except Exception as e:
                try:
                    db.append_event(trade_id, "TP_ERROR", f"TP{idx} failed: {e}")
                except Exception:
                    pass
                telemetry.log("exec", "TP_ERROR", str(e), {"symbol": sym_name, "engine": engine, "exchange": exch_id, "tp_idx": idx})
        telemetry.log("exec", "TP_SPLIT_PLACED", "live split-TPs placed", {"trade_id": trade_id, "tps": tp_summ})
    elif C.PLACE_TP3_LIMIT and tp3_px is not None:
        try:
            tp_order = ex.create_order(
                symbol,
                type="limit",
                side=side_exit,
                amount=qty,
                price=tp3_px,
                params={"reduceOnly": True}
            )
            oid = tp_order.get("id", "")
            order_ids.append(oid)
            db.add_order(trade_id, oid, "take_profit_final", side_exit, tp3_px, qty, "open")
        except Exception as e:
            try:
                db.append_event(trade_id, "TP3_ERROR", f"TP3 failed: {e}")
            except Exception:
                pass
            telemetry.log("exec", "TP3_ERROR", str(e), {"symbol": sym_name, "engine": engine, "exchange": exch_id})

    telemetry.log("exec", "LIVE_ORDERS",
                  f"{sig.side} live orders placed",
                  {"trade_id": trade_id, "entry": entry_px, "sl": sl_px, "tp3": tp3_px, "qty": qty,
                   "order_ids": order_ids, "engine": engine, "exchange": exch_id, "symbol": sym_name})
    return order_ids