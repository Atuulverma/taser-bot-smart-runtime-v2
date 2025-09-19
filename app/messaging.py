from typing import Any, Optional, Protocol, cast

from . import config as C


class _TelemetryProto(Protocol):
    def log(self, component: str, tag: str, message: str, payload: dict[str, Any]) -> None: ...


_telemetry: Optional[_TelemetryProto] = None

try:
    from . import telemetry as _telemetry_mod  # runtime import

    _telemetry = cast(_TelemetryProto, _telemetry_mod)
except Exception:
    _telemetry = None
# Determine enabled engines from config
_def_order = ["trendscalp"]
try:
    _order_src = getattr(C, "ENGINE_ORDER", _def_order) or _def_order
    _ENGINE_ORDER = [s.strip().lower() for s in _order_src]
    if isinstance(_ENGINE_ORDER, str):
        _ENGINE_ORDER = [s.strip().lower() for s in _ENGINE_ORDER.split(",") if s.strip()]
except Exception:
    _ENGINE_ORDER = _def_order


def _trendscalp_is_only_engine(meta: dict) -> bool:
    eng = (meta or {}).get("engine", "").lower()
    only_ts = _ENGINE_ORDER == ["trendscalp"] or (
        len(_ENGINE_ORDER) == 1 and _ENGINE_ORDER[0] == "trendscalp"
    )
    return only_ts or (eng == "trendscalp")


def _dbg_meta_block(meta: dict, note: str = "") -> str:
    if not getattr(C, "TG_DEBUG_VALIDATORS", False):
        return ""
    m = dict(meta or {})
    keys = sorted(list(m.keys()))
    state, cfg = _coalesce_state_cfg(m) if "_coalesce_state_cfg" in globals() else ({}, {})
    s_keys = sorted(list(state.keys())) if isinstance(state, dict) else []
    c_keys = sorted(list(cfg.keys())) if isinstance(cfg, dict) else []
    lines = ["\nDEBUG:"]
    lines.append(f"• note: {note}" if note else "• validators missing")
    keys_str = ", ".join(keys[:20])
    s_keys_str = ", ".join(s_keys[:20])
    c_keys_str = ", ".join(c_keys[:20])
    lines.append(f"• meta.keys: {keys_str}{' …' if len(keys) > 20 else ''}")
    lines.append(f"• state.keys: {s_keys_str}{' …' if len(s_keys) > 20 else ''}")
    lines.append(f"• cfg.keys: {c_keys_str}{' …' if len(c_keys) > 20 else ''}")
    # try to surface common fields even if paths changed
    probe = {
        "atr14_last": m.get("atr14_last") or state.get("atr14_last"),
        "adx_last": m.get("adx_last") or state.get("adx_last"),
        "rsi15": m.get("rsi15") or state.get("rsi15"),
        "ema200_5": m.get("ema200_5") or state.get("ema200_5"),
        "ema200_15": m.get("ema200_15") or state.get("ema200_15"),
        "regime_ok": m.get("regime_ok") or state.get("regime_ok"),
        "vol_ok": m.get("vol_ok") or state.get("vol_ok"),
    }
    found = [f"{k}={probe[k]}" for k in probe if probe[k] is not None]
    if found:
        lines.append("• probe: " + ", ".join(found))
    out = "\n".join(lines)
    # also mirror to telemetry if available
    try:
        if _telemetry is not None:
            _telemetry.log("tgdebug", "VALIDATORS_MISSING", out, {})
    except Exception:
        pass
    return out


def _ensure_ts_meta(meta: dict, price: Optional[float] = None) -> dict:
    """Ensure meta has sensible defaults for TrendScalp messaging."""
    m = dict(meta or {})
    if not m.get("engine"):
        m["engine"] = "trendscalp"
    # Inject price whenever caller provided one and meta price is missing or None
    if price is not None and (("price" not in m) or (m.get("price") is None)):
        try:
            m["price"] = float(price)
        except Exception:
            m["price"] = price
    # Backfill legacy shapes so downstream formatters always find what they need
    if "filter_state" not in m:
        if isinstance(m.get("validators"), dict):
            m["filter_state"] = dict(m["validators"])
        elif isinstance(m.get("filters"), dict):
            m["filter_state"] = dict(m["filters"])
    if "filter_cfg" not in m:
        if isinstance(m.get("cfg"), dict):
            m["filter_cfg"] = dict(m["cfg"])
        elif isinstance(m.get("config"), dict):
            m["filter_cfg"] = dict(m["config"])
    try:
        if m.get("price") is not None:
            m["price"] = float(m["price"])
    except Exception:
        pass
    return m


def _coalesce_state_cfg(meta: dict) -> tuple:
    """
    Robustly derive (state, cfg) for TrendScalp messaging.
    Accepts multiple legacy shapes:
      - filter_state/filter_cfg (current)
      - validators/filters (older alias for state)
      - cfg/config (older alias for cfg)
    """
    m = dict(meta or {})

    # 1) Preferred keys
    state = m.get("filter_state")
    cfg = m.get("filter_cfg")

    # 2) Legacy aliases (older code paths)
    if not isinstance(state, dict):
        if isinstance(m.get("validators"), dict):
            state = m.get("validators")
        elif isinstance(m.get("filters"), dict):
            state = m.get("filters")
        else:
            state = None
    if not isinstance(cfg, dict):
        if isinstance(m.get("cfg"), dict):
            cfg = m.get("cfg")
        elif isinstance(m.get("config"), dict):
            cfg = m.get("config")
        else:
            cfg = None

    # 3) Additional common variants observed in field (non-breaking):
    #    - 'state' for filter state
    #    - 'settings' for config
    if not isinstance(state, dict):
        state = m.get("state") if isinstance(m.get("state"), dict) else None
    if not isinstance(cfg, dict):
        cfg = m.get("settings") if isinstance(m.get("settings"), dict) else None

    # 4) Last-resort: reconstruct a minimal state from flat meta fields
    if not isinstance(state, dict) or not state:
        state = _raw_state_from_meta(m)

    # 5) Last-resort: default cfg so thresholds render (vol floor / ADX / TL-width mult)
    if not isinstance(cfg, dict) or not cfg:
        cfg = _default_ts_cfg()

    return (state or {}), (cfg or {})


# === Inserted helpers for TrendScalp config/state backfill ===
def _default_ts_cfg() -> dict:
    """Fallback TrendScalp filter config from env-config if caller didn't pass cfg."""
    return {
        "TS_VOL_FLOOR_PCT": float(getattr(C, "TS_VOL_FLOOR_PCT", 0.0012)),
        "TS_ADX_MIN": float(getattr(C, "TS_ADX_MIN", 20)),
        "TS_TL_WIDTH_ATR_MULT": float(getattr(C, "TS_TL_WIDTH_ATR_MULT", 0.42)),
    }


def _raw_state_from_meta(m: dict) -> dict:
    """Build a best-effort state dict from flat meta keys when filter_state is missing."""
    m = dict(m or {})
    s = {}
    for k in (
        "atr14_last",
        "adx_last",
        "rsi15",
        "ema200_5",
        "ema200_15",
        "regime_ok",
        "vol_ok",
        "adx_ok",
        "rsi_block",
        "ma_long_ok",
        "ma_short_ok",
        "upper_break",
        "lower_break",
        "ema_up",
        "ema_dn",
        "tl_width",
    ):
        if m.get(k) is not None:
            s[k] = m.get(k)
    return s


# === Inserted: Compact debug of what messaging.py received from the caller ===
def _dbg_rx(func_name: str, price: Optional[float], meta: dict) -> str:
    """
    Compact debug of what messaging.py received from the caller (e.g., trendscalp/taser_rules).
    Always mirrors to telemetry (channel: tgdebug | event: MSG_INPUT) when TG_DEBUG_VALIDATORS=True.
    Returns a short text block you can embed into TG messages.
    """
    if not getattr(C, "TG_DEBUG_VALIDATORS", False):
        return ""
    try:
        m = _ensure_ts_meta(meta, price)
    except Exception:
        m = dict(meta or {})
        if price is not None:
            m.setdefault("price", price)
    state, cfg = _coalesce_state_cfg(m)
    meta_keys = sorted(list(m.keys()))[:20]
    state_keys = sorted(list(state.keys()))[:20] if isinstance(state, dict) else []
    cfg_keys = sorted(list(cfg.keys()))[:20] if isinstance(cfg, dict) else []
    # surface most important probes if present
    probe = {
        "atr14_last": (state or {}).get("atr14_last", m.get("atr14_last")),
        "adx_last": (state or {}).get("adx_last", m.get("adx_last")),
        "rsi15": (state or {}).get("rsi15", m.get("rsi15")),
        "ema200_5": (state or {}).get("ema200_5", m.get("ema200_5")),
        "ema200_15": (state or {}).get("ema200_15", m.get("ema200_15")),
        "regime_ok": (state or {}).get("regime_ok", m.get("regime_ok")),
        "vol_ok": (state or {}).get("vol_ok", m.get("vol_ok")),
    }
    probe_found = {k: v for k, v in probe.items() if v is not None}
    # mirror to telemetry
    try:
        if _telemetry is not None:
            _telemetry.log(
                "tgdebug",
                "MSG_INPUT",
                f"{func_name} received — engine={m.get('engine', '?')} price={m.get('price')}",
                {
                    "func": func_name,
                    "engine": (m or {}).get("engine"),
                    "price": (m or {}).get("price"),
                    "meta_keys": meta_keys,
                    "state_keys": state_keys,
                    "cfg_keys": cfg_keys,
                    "probe": probe_found,
                },
            )
    except Exception:
        pass
    # build small inline block for TG
    lines = [
        "\nDEBUG RX:",
        f"• from: {func_name}",
        f"• meta.keys: {', '.join(meta_keys)}{' …' if len(meta_keys) == 20 else ''}",
        f"• state.keys: {', '.join(state_keys)}{' …' if len(state_keys) == 20 else ''}",
        f"• cfg.keys: {', '.join(cfg_keys)}{' …' if len(cfg_keys) == 20 else ''}",
    ]
    if probe_found:
        pf = ", ".join([f"{k}={probe_found[k]}" for k in probe_found.keys()])
        lines.append(f"• probe: {pf}")
    return "\n".join(lines)


def fmt_levels(meta):
    bits = []
    if meta.get("pdh"):
        bits.append(f"PDH {meta['pdh']:.4f}")
    if meta.get("pdl"):
        bits.append(f"PDL {meta['pdl']:.4f}")
    if meta.get("vwap5"):
        bits.append(f"VWAP {meta['vwap5']:.4f}")
    if meta.get("avwap_hi"):
        bits.append(f"AVWAP↑ {meta['avwap_hi']:.4f}")
    if meta.get("avwap_lo"):
        bits.append(f"AVWAP↓ {meta['avwap_lo']:.4f}")
    return " | ".join(bits)


def fmt_validators_trendscalp(meta):
    state, cfg = _coalesce_state_cfg(meta)
    parts = []
    use_rsi_filter = bool(getattr(C, "TS_USE_RSI_FILTER", True))
    use_regime_filter = bool(getattr(C, "TS_USE_REGIME_FILTER", True))
    # ML line (always show a token so validators row prints)
    use_ml = bool(getattr(C, "TS_USE_ML_GATE", False))
    if use_ml:
        bias = str(state.get("ml_bias", "neutral"))
        try:
            conf = float(state.get("ml_conf", 0.0))
        except Exception:
            conf = 0.0
        thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
        ok_ml = (bias in ("long", "short")) and (conf >= thr)
        parts.append(f"ML {bias} {conf:.2f}≥{thr:.2f} {'✓' if ok_ml else '✗'}")
    else:
        parts.append("ML (off)")
    # ATR floor
    atr = state.get("atr14_last")
    floor = cfg.get("TS_VOL_FLOOR_PCT")
    px = (meta or {}).get("price")
    vol_ok = state.get("vol_ok")
    if atr is not None and floor is not None and px:
        pct = (float(atr) / max(1e-9, float(px))) * 100.0
        if vol_ok is None:
            vol_ok = pct >= float(floor) * 100.0
        parts.append(f"ATRfloor {pct:.2f}% ≥ {float(floor) * 100:.2f}% {'✓' if vol_ok else '✗'}")
    elif vol_ok is not None:
        parts.append(f"ATRfloor {'✓' if vol_ok else '✗'}")
    # ADX (soft-aware)
    if state.get("adx_last") is not None and cfg.get("TS_ADX_MIN") is not None:
        adx_last = float(state.get("adx_last", 0.0))
        adx_min = float(cfg.get("TS_ADX_MIN", adx_last))
        soft_ok = bool(state.get("adx_ok_soft"))
        strict_ok = adx_last >= adx_min
        adx_ok = bool(state.get("adx_ok", strict_ok))
        note = " (soft)" if (adx_ok and not strict_ok and soft_ok) else ""
        parts.append(f"ADX {adx_last:.1f}≥{adx_min:.0f}{note} {'✓' if adx_ok else '✗'}")
    # RSI15 side-bias (honor toggle)
    if state.get("rsi15") is not None:
        rsi15 = float(state["rsi15"])
        if not use_rsi_filter:
            parts.append(f"RSI15 {rsi15:.1f} (disabled)")
        else:
            if state.get("rsi_block"):
                parts.append(f"RSI15 {rsi15:.1f} (45–55 block) ✗")
            else:
                parts.append(f"RSI15 {rsi15:.1f} (side-bias ok) ✓")
    # EMA200 alignment (5m & 15m)
    e5 = state.get("ema200_5")
    if e5 is not None:
        if state.get("ma_long_ok") is not None or state.get("ma_short_ok") is not None:
            aligned = bool(state.get("ma_long_ok") or state.get("ma_short_ok"))
            parts.append("EMA200(5/15) aligned ✓" if aligned else "EMA200(5/15) misaligned ✗")
        else:
            parts.append("EMA200(5/15) present")
    # Regime width (honor toggle)
    if cfg.get("TS_TL_WIDTH_ATR_MULT") is not None:
        if not use_regime_filter:
            parts.append("Regime (disabled)")
        elif state.get("regime_ok") is not None:
            parts.append(f"Regime (TLwidth vs ATR) {'✓' if state.get('regime_ok') else '✗'}")
    return " | ".join(parts)


def fmt_details_trendscalp(meta):
    state, cfg = _coalesce_state_cfg(meta)
    p = (meta or {}).get("price")
    use_rsi_filter = bool(getattr(C, "TS_USE_RSI_FILTER", True))
    use_regime_filter = bool(getattr(C, "TS_USE_REGIME_FILTER", True))
    if not state:
        return ""
    lines = []
    # Row 1: volatility / trend / bias
    atr = state.get("atr14_last")
    floor = cfg.get("TS_VOL_FLOOR_PCT")
    adx = state.get("adx_last")
    adx_min = cfg.get("TS_ADX_MIN")
    rsi15 = state.get("rsi15")
    rsi_blk = state.get("rsi_block")
    r1 = []
    if atr is not None and floor is not None and p:
        pct = (float(atr) / max(1e-9, float(p))) * 100.0
        r1.append(f"ATR14 {float(atr):.4f} ({pct:.2f}% ≥ {(float(floor) * 100):.2f}%)")
    if adx is not None:
        if adx_min is not None:
            r1.append(f"ADX {float(adx):.1f}≥{int(adx_min)}")
        else:
            r1.append(f"ADX {float(adx):.1f}")
    if rsi15 is not None:
        if not use_rsi_filter:
            r1.append(f"RSI15 {float(rsi15):.1f} (disabled)")
        else:
            r1.append(f"RSI15 {float(rsi15):.1f}{' block' if rsi_blk else ''}")
    if r1:
        lines.append(" | ".join(r1))
    # Row 2: structure / regime
    e5 = state.get("ema200_5")
    e15 = state.get("ema200_15")
    tlw = state.get("tl_width")
    mult = cfg.get("TS_TL_WIDTH_ATR_MULT")
    r2 = []
    if e5 is not None:
        r2.append(f"EMA200(5m) {float(e5):.4f}")
    if e15 is not None:
        r2.append(f"EMA200(15m) {float(e15):.4f}")
    if mult is not None:
        if not use_regime_filter:
            r2.append("Regime disabled")
        elif tlw is not None and atr is not None:
            r2.append(
                f"TLw {float(tlw):.4f} vs {float(mult):.2f}×ATR {float(mult) * float(atr):.4f}"
            )
    if r2:
        lines.append(" | ".join(r2))
    # Row 3: alignment / triggers (booleans kept terse)
    r3 = []
    for k in ("ma_long_ok", "ma_short_ok", "upper_break", "lower_break", "ema_up", "ema_dn"):
        if k in state:
            r3.append(f"{k} {'✓' if state.get(k) else '✗'}")
    if r3:
        lines.append(" | ".join(r3))
    return "\n".join(lines)


def fmt_validators(meta):
    # If TrendScalp is the only engine (or meta explicitly says trendscalp), show TrendScalp gates
    if _trendscalp_is_only_engine(meta) or (
        meta and any(k in meta for k in ("filter_state", "validators", "filters"))
    ):
        try:
            return fmt_validators_trendscalp(meta)
        except Exception:
            pass
    v = []
    if meta.get("delta_pos") is not None:
        v.append(f"Δ (CVD) {'+' if meta['delta_pos'] else '−'}")
    if meta.get("oi_up") is not None:
        v.append(f"OI {'↑' if meta['oi_up'] else '↘'}")
    if meta.get("rsi") is not None:
        v.append(f"RSI {meta['rsi']:.1f}")
    if meta.get("macd_hist") is not None:
        v.append(f"MACDhist {meta['macd_hist']:.4f}")
    return " | ".join(v)


def fmt_avoid(meta):
    zones = (meta or {}).get("avoid_zones") or []
    if not zones:
        return ""
    return ", ".join([f"{lo:.4f}-{hi:.4f}" for (lo, hi) in zones])


# --- Regime line helper for TrendScalp messages ---
def _fmt_regime_line(meta: dict) -> str:
    """Return a single-line 'Regime: RUNNER|CHOP' if available, else empty string."""
    try:
        r = (meta or {}).get("regime")
        if not r and isinstance(meta, dict):
            # Try nested in state (rare older paths)
            st = (meta or {}).get("filter_state") or (meta or {}).get("validators") or {}
            r = st.get("regime") if isinstance(st, dict) else None
        if r:
            return f"Regime: {str(r).upper()}\n"
    except Exception:
        pass
    return ""


def suggest_next_step_trendscalp(meta):
    state, cfg = _coalesce_state_cfg(meta)
    need = []
    use_rsi_filter = bool(getattr(C, "TS_USE_RSI_FILTER", True))
    use_regime_filter = bool(getattr(C, "TS_USE_REGIME_FILTER", True))
    # Map failed gates to actionable guidance
    if use_rsi_filter and state.get("rsi_block"):
        need.append("• RSI(15m) must leave 45–55; >50 for LONG, <50 for SHORT.")
    if (
        state.get("vol_ok") is False
        and cfg.get("TS_VOL_FLOOR_PCT") is not None
        and meta.get("price")
        and state.get("atr14_last") is not None
    ):
        pct = (float(state["atr14_last"]) / max(1e-9, float(meta["price"]))) * 100.0
        need.append(
            "• Volatility: ATR14(5m)/Price "
            f"{pct:.2f}% < {float(cfg['TS_VOL_FLOOR_PCT']) * 100:.2f}%"
            " — wait for ≥ threshold."
        )
    if (
        state.get("adx_ok") is False
        and cfg.get("TS_ADX_MIN") is not None
        and state.get("adx_last") is not None
    ):
        need.append(
            "• Trend strength: ADX(5m) "
            f"{float(state['adx_last']):.1f} < {float(cfg['TS_ADX_MIN']):.0f}"
            " — wait for ≥ threshold."
        )
    if (
        use_regime_filter
        and state.get("regime_ok") is False
        and cfg.get("TS_TL_WIDTH_ATR_MULT") is not None
    ):
        need.append(
            "• Regime: TL channel width must be ≥ "
            f"{float(cfg['TS_TL_WIDTH_ATR_MULT']):.2f}×ATR14(5m)."
        )
    if (state.get("ma_long_ok") is False) and (state.get("ma_short_ok") is False):
        need.append(
            "• Align with trend: price must be on the correct side of 200‑EMA (5m & 15m) "
            "for the trade side."
        )
    # Fallback guidance if nothing explicit is set
    if not need:
        need.append(
            "• Wait for TrendScalp gates to pass (ATR/ADX/RSI15/EMA/Regime) and a TL break "
            "in the trade direction."
        )
    return "\n".join(need)


def suggest_next_step(price, meta):
    if _trendscalp_is_only_engine(meta):
        try:
            # Inject price for ATRfloor % display if available
            if meta is not None:
                meta = dict(meta)
                meta.setdefault("price", price)
            return suggest_next_step_trendscalp(meta)
        except Exception:
            pass
    pdh = meta.get("pdh")
    vwap5 = meta.get("vwap5")
    avhi = meta.get("avwap_hi")
    avlo = meta.get("avwap_lo")
    need = []
    if pdh:
        if price <= pdh:
            need.append(
                f"• Long: breakout + hold > PDH {pdh:.4f} with Δ+ & OI↑; SL below PDH/VWAP reclaim."
            )
        else:
            need.append(
                f"• Short: rejection near PDH {pdh:.4f} with Δ− or OI↘; SL just above wick."
            )
    if vwap5:
        need.append(f"• VWAP: reclaim/lose VWAP {vwap5:.4f} with confirming orderflow.")
    if avhi:
        need.append(
            f"• Watch AVWAP↑ {avhi:.4f} for stop-run + fail (short) or clean reclaim (long)."
        )
    if avlo:
        need.append(f"• Watch AVWAP↓ {avlo:.4f} for sweep + reclaim (long).")
    if not need:
        need = ["• Wait for price to reach PDH/PDL/VWAP/AVWAP with Δ & OI confirmation."]
    return "\n".join(need)


def no_trade_message(price, reason, meta):
    m = _ensure_ts_meta(meta, price)
    rx_block = _dbg_rx("no_trade_message", price, m)
    elig = m.get("eligibility", {})
    extra = ""
    if elig:

        def fmt(x):
            return "—" if x is None else f"{x * 100:.2f}%"

        extra = (
            f"\nEligibility: dist→VWAP {fmt(elig.get('dist_to_vwap_pct'))}, "
            f"dist→AVWAP↑ {fmt(elig.get('dist_to_avhi_pct'))}, "
            f"dist→AVWAP↓ {fmt(elig.get('dist_to_avlo_pct'))}, "
            f"bias L/S {elig.get('long_bias')}/{elig.get('short_bias')}"
        )
    validators_str = fmt_validators(m)
    validators_line = f"Validators: {validators_str}\n" if validators_str else ""
    details_str = (
        fmt_details_trendscalp(m)
        if (_trendscalp_is_only_engine(m) or ("filter_state" in m))
        else ""
    )
    details_block = (details_str + "\n") if details_str else ""
    avoid = fmt_avoid(m)
    avoid_line = "" if not avoid else f"Avoid zones: {avoid}\n"
    debug_block = ""
    if not validators_line:
        debug_block = _dbg_meta_block(m, note="no_trade_message")
    regime_line = _fmt_regime_line(m)
    return (
        f"🚫 NO TRADE — {C.PAIR}\n"
        f"Engine: {(m or {}).get('engine', '—')}\n"
        f"Price: {price:.4f}\n"
        f"Reason: {reason}\n"
        f"Levels: {fmt_levels(m)}\n"
        f"{regime_line}"
        f"{validators_line}"
        f"{details_block}"
        f"{avoid_line}"
        f"{rx_block}"
        f"{debug_block}\n"
        f"What we need next:\n{suggest_next_step(price, m)}{extra}"
    )


def signal_message(sig):
    m = _ensure_ts_meta(getattr(sig, "meta", {}) or {}, getattr(sig, "entry", None))
    rx_block = _dbg_rx("signal_message", getattr(sig, "entry", None), m)
    validators_str = fmt_validators(m)
    validators_line = f"Validators: {validators_str}\n" if validators_str else ""
    details_str = (
        fmt_details_trendscalp(m)
        if (_trendscalp_is_only_engine(m) or ("filter_state" in m))
        else ""
    )
    details_block = (details_str + "\n") if details_str else ""
    debug_block = ""
    if not validators_line:
        debug_block = _dbg_meta_block(m, note="signal_message")
    tps_str = ", ".join([f"{t:.4f}" for t in sig.tps])
    regime_line = _fmt_regime_line(m)
    return (
        f"✅ {sig.side} APPROVED — {C.PAIR}\n"
        f"Engine: {m.get('engine', '—')}\n"
        f"Entry {sig.entry:.4f} | SL {sig.sl:.4f} | TP {tps_str}\n"
        f"Reason: {sig.reason}\n"
        f"Levels: {fmt_levels(m)}\n"
        f"{regime_line}"
        f"{validators_line}"
        f"{details_block}"
        f"{rx_block}"
        f"{debug_block}\n"
    )


def invalidation_message(reason, draft, price):
    m = _ensure_ts_meta(getattr(draft, "meta", {}) or {}, price)
    rx_block = _dbg_rx("invalidation_message", price, m)
    details_str = (
        fmt_details_trendscalp(m)
        if (_trendscalp_is_only_engine(m) or ("filter_state" in m))
        else ""
    )
    details_block = (details_str + "\n") if details_str else ""
    debug_block = _dbg_meta_block(m, note="invalidation_message")
    regime_line = _fmt_regime_line(m)
    return (
        f"⚠️ INVALIDATED — {C.PAIR}\n"
        f"Engine: {m.get('engine', '—')}\n"
        f"Side: {draft.side} | Last {price:.4f}\n"
        f"Reason: {reason}\n"
        f"{regime_line}"
        f"{details_block}"
        f"{rx_block}"
        f"{debug_block}\n"
        f"Next:\n{suggest_next_step(price, m)}"
    )


def extension_message(draft, price):
    return (
        f"📈 PROFIT EXTENSION — {C.PAIR}\n"
        f"Engine: {(getattr(draft, 'meta', {}) or {}).get('engine', '—')}\n"
        f"{draft.side} running. Last {price:.4f} > TP3. Added reduce-only TP.\n"
        f"Context: {fmt_levels(draft.meta)} | {fmt_validators(draft.meta)}"
    )


def _manual_close_context_line(draft, price_now):
    meta_now = _ensure_ts_meta(getattr(draft, "meta", {}) or {}, price_now)
    return f"Context: {fmt_levels(meta_now)} | {fmt_validators(meta_now)}"


def manual_close_message(pair, exit_px, pnl, draft, price_now):
    return (
        f"🧑‍💻 MANUAL CLOSE DETECTED — {pair}\n"
        f"Engine: {(getattr(draft, 'meta', {}) or {}).get('engine', '—')}\n"
        f"Exit {exit_px:.4f} | PnL {pnl:.2f}\n"
        f"We’ll wait for the next valid setup.\n"
        f"Next:\n{suggest_next_step(price_now, draft.meta)}\n"
        f"{_manual_close_context_line(draft, price_now)}"
    )


def audit_block_message(draft, verdict):
    why = (verdict or {}).get("why", "").strip()
    why = why if why else "Not approved by auditor"
    return (
        f"🛑 AUDIT BLOCKED — {C.PAIR}\n"
        f"Engine: {(getattr(draft, 'meta', {}) or {}).get('engine', '—')}\n"
        f"Proposed: {draft.side} @ {draft.entry:.4f} | SL {draft.sl:.4f} | TPs {draft.tps}\n"
        f"Reason: {why}"
    )
