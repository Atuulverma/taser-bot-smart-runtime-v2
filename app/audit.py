# app/audit.py
from typing import Any, Dict

from . import config as C

# --- Optional OpenAI wiring (disabled by default) ---
# from openai import AsyncOpenAI
# _OPENAI = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY")) if os.getenv(
#     "OPENAI_API_KEY") else None
# _OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5")
# _OPENAI_ENABLED = os.getenv("OPENAI_USE", "false").lower() == "true" and _OPENAI is not None


def _tail(tf: dict, n: int) -> dict:
    return {k: (v[-n:] if isinstance(v, list) else v) for k, v in tf.items()}


def _build_audit_payload(draft, tf5, tf15, tf1h) -> Dict[str, Any]:
    try:
        m = draft.meta or {}
        price = float(tf5["close"][-1])
        vwp = m.get("vwap5")
        avhi = m.get("avwap_hi")
        avlo = m.get("avwap_lo")

        def dist(x: float | None) -> float | None:
            return (abs(price - x) / price) if (x is not None) else None

        macd_hist_val = None
        macd_hist_data = m.get("macd_hist")
        if isinstance(macd_hist_data, list):
            macd_hist_val = macd_hist_data[-1]
        else:
            macd_hist_val = macd_hist_data

        return {
            "pair": C.PAIR,
            "mode": "LIVE" if (not C.DRY_RUN) else "PAPER",
            "ts": int(tf5["timestamp"][-1]),
            "rule": str(draft.reason),
            "price": price,
            "levels": {
                "pdh": m.get("pdh"),
                "pdl": m.get("pdl"),
                "vwap5": vwp,
                "avwap_hi": avhi,
                "avwap_lo": avlo,
            },
            "dist_pct": {
                "to_vwap": dist(vwp),
                "to_avhi": dist(avhi),
                "to_avlo": dist(avlo),
            },
            "validators": {
                "delta_pos": m.get("delta_pos"),
                "oi_up": m.get("oi_up"),
                "rsi": m.get("rsi"),
                "macd_hist": macd_hist_val,
                "atr": m.get("atr"),
                "atr_pct": m.get("atr_pct"),
            },
            "avoid": {
                "zones": m.get("avoid_zones") or [],
                "debug": m.get("avoid_debug") or {},
            },
            "heatmap": m.get("heatmap_levels") or [],
            "ohlcv_tail": {
                "5m": _tail(tf5, 50),
                "15m": _tail(tf15, 50),
                "1h": _tail(tf1h, 50),
            },
            "plan": {
                "side": draft.side,
                "entry": draft.entry,
                "sl": draft.sl,
                "tps": draft.tps,
            },
        }
    except Exception as e:
        return {
            "pair": C.PAIR,
            "mode": "LIVE" if (not C.DRY_RUN) else "PAPER",
            "ts": int(tf5["timestamp"][-1]) if tf5 and "timestamp" in tf5 else None,
            "rule": str(getattr(draft, "reason", "")),
            "price": float(tf5["close"][-1]) if tf5 and "close" in tf5 and tf5["close"] else None,
            "_error": f"payload-build: {e.__class__.__name__}: {e}",
        }


async def approve_with_rationale(draft, tf5, tf15, tf1h) -> Dict[str, Any]:
    payload = _build_audit_payload(draft, tf5, tf15, tf1h)

    # --- Disabled OpenAI block ---
    # if _OPENAI_ENABLED:
    #     try:
    #         system = (
    #             "You are a crypto intraday risk co-pilot. The draft trade has already passed "
    #             "a deterministic rule engine. Return JSON with fields: "
    #             "{decision: APPROVE|WAIT|REJECT, why: short, notes: optional}."
    #         )
    #         user = {
    #             "summary": (
    #                 f"{payload.get('plan', {}) .get('side')} {payload.get('pair')} "
    #                 f"@ {payload.get('plan', {}) .get('entry')} "
    #                 f"SL {payload.get('plan', {}) .get('sl')} "
    #                 f"TPs {payload.get('plan', {}) .get('tps')} "
    #                 f"rule={payload.get('rule')}"
    #             ),
    #             "context": payload,
    #         }
    #         resp = await _OPENAI.chat.completions.create(
    #             model=_OPENAI_MODEL,
    #             messages=[{"role":"system","content":system},
    #                       {"role":"user","content":json.dumps(user)}],
    #             temperature=0.1,
    #         )
    #         text = resp.choices[0].message.content if resp and resp.choices else ""
    #         parsed = json.loads(text)
    #         parsed["_ctx"] = payload
    #         if "decision" not in parsed:
    #             parsed["decision"] = "WAIT"
    #             parsed["why"] = parsed.get("why","missing decision in reply")
    #         return parsed
    #     except Exception as e:
    #         return {"decision":"WAIT","why":f"auditor exception: {e}", "_ctx": payload}

    # --- Always approve (stub path) ---
    return {"decision": "APPROVE", "why": "stub approval — OpenAI audit disabled", "_ctx": payload}
