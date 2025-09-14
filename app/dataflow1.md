# Taser Bot — End‑to‑End Dataflow (Deep Dive)

_Last updated: 2025‑09‑08_  
_Scope: `trendscalp` engine + shared infra (`indicators`, `data`, `scheduler`, `messaging`, `surveillance`, `execution`)._

## 0) TL;DR (what’s broken & why)
- **Symptoms**: TG shows `NO_TRADE` messages **without validator details** (ATR/ADX/RSI/EMA/Regime), e.g. `state.keys:` and `cfg.keys:` are empty or partial. Entries are late / skipped; SL management feels loose.
- **Root cause (primary)**: The **messaging contract** from scanner → `messaging.no_trade_message(...)` is not carrying the expected `state` and `cfg` dicts. We only forward `meta`. The old path used to bundle validator outputs from `trendscalp.scan()` into `state` and thresholds into `cfg`. That linkage got broken during refactors.
- **Root cause (secondary)**: A runtime error appears in logs: `scalp ERROR unsupported operand type(s) for -: 'float' and 'NoneType'`. This indicates **one of the indicators needed by TrendScalp is `None`**, most likely a missing EMA/RSI/ADX in the first bars after startup/backfill or a guard that didn’t handle empty series.
- **Noise**: TP spam previously came from repeated identical tuples; now gated by EPS & cooldown, but we’ll re‑verify once entries happen again.

**Immediate fix path (no code here, just plan):**
1) In **`scheduler.py`**: when a scan yields `NO_TRADE`, include **both** `state` (computed validators) and **`cfg`** (thresholds/knobs) in the payload to `messaging.no_trade_message`. (We already added debug to print the missing keys.)
2) In **`trendscalp.py`**: ensure `scan()` returns a structured object/dict with fields: `price`, `side` (or `None`), `reason`, `state` (atr_pct, adx, rsi15, ema_up/dn, tl_width, breaks, etc.), `cfg` (TS_VOL_FLOOR_PCT, TS_ADX_MIN, TS_TL_WIDTH_ATR_MULT, etc.), and `meta` (pdh/pdl/heatmap levels). Guard against `None` indicators.
3) In **`indicators.py`**: return numeric defaults or explicitly mark `ready=False` until minimum bars loaded; callers must branch on readiness to avoid `None` math.

---

## 1) High‑level flow

```
[scheduler.scan_loop]
   └─ fetch OHLCV via [data.py]
   └─ compute features via [indicators.py]
   └─ run engine [trendscalp.scan]
        ├─ decide NO_TRADE / LONG / SHORT
        ├─ package (meta, state, cfg, reason, price, levels)
        └─ return ScanDecision
   └─ for NO_TRADE → [messaging.no_trade_message]
   └─ for TRADE → [execution.place_bracket] and start [surveillance.surveil_loop]

[surveillance.surveil_loop]
   ├─ manage SL/TP trail (heatmap/flow/ratchet)
   ├─ post events via [telemetry]
   └─ TP updates via [tp_orders] (anti‑spam) (if enabled)
```

> **Contracts matter**: `trendscalp.scan()` must emit **state/cfg**; `scheduler` must forward them to `messaging`. Today only `meta` survives.

---

## 2) Module deep‑dive

### 2.1 `indicators.py` (producers)
- **Inputs**: OHLCV arrays (`open/high/low/close/volume`) for 5m/15m/1h; lookback sizes are defined by config.
- **Outputs** (used by TrendScalp):
  - `ATR14 (5m)` and ATR percentage vs price → `atr_pct`
  - `ADX (14)` → `adx`
  - `RSI (15m)` → `rsi15`
  - `EMA200(5m)`, `EMA200(15m)` and alignment flags: `ema_up`, `ema_dn`
  - Optional: trendline width (`tl_width`) / regime proxy
- **Readiness**: requires N bars; until then values may be `None`. **Action**: return `ready=False` or guard computations in TrendScalp to prevent `None` arithmetic.

### 2.2 `data.py` (transport & cache)
- **Fetch**: wraps ccxt OHLCV & possibly higher TF backfill. Emits dicts with lists; timestamps in ms (converted to IST in logs downstream).
- **Cache**: may hold recent windows for 5m/15m/1h to avoid over‑fetch. **Action**: ensure windows cover min bars for all indicators.

### 2.3 `trendscalp.py` (decision engine)
- **Consumes**: features from `indicators` on 5m/15m/1h; heatmap hints from data/meta; PDH/PDL; config thresholds.
- **Decides**: NO_TRADE vs LONG/SHORT with entry, SL sizing rails (`MIN_SL_PCT..MAX_SL_PCT`), TP mode (R‑based by default).
- **Must return (contract)**:
  ```python
  ScanDecision = {
    "engine": "trendscalp",
    "price": float,
    "side": "LONG"|"SHORT"|None,
    "reason": "NO_EDGE"|"…",
    "meta": {pdh,pdl,heatmap_levels_*…},
    "state": {atr_pct, adx, rsi15, ema_up, ema_dn, tl_width, ma_long_ok, ma_short_ok, upper_break, lower_break},
    "cfg": {TS_VOL_FLOOR_PCT, TS_ADX_MIN, TS_TL_WIDTH_ATR_MULT, …},
    "tps": [tp1,tp2,tp3] (if trade),
    "sl": float (if trade)
  }
  ```
- **Finding**: logs show `state` often **empty**. Likely not set, or stripped before sending.

### 2.4 `scheduler.py` (orchestrator)
- Calls `trendscalp.scan()` per tick.
- **For NO_TRADE**: must call `messaging.no_trade_message(engine, price, reason, levels, meta, state, cfg)` and pass **all dicts** unchanged.  
  **Finding**: current behavior passes `meta` but leaves `state/cfg` empty → TG lacks validator detail.
- **For TRADE**: routes to `execution.place_bracket()` and then `surveillance.surveil_loop(...)` with draft/meta.

### 2.5 `messaging.py` (presentation)
- Renders TG messages; expects `meta`, `state`, `cfg`.  
- **Finding**: after adding debug, we see `state.keys:` and `cfg.keys:` often empty. When non‑empty, keys match `TS_ADX_MIN`, `TS_TL_WIDTH_ATR_MULT`, `TS_VOL_FLOOR_PCT` (cfg), but still **no `state`** (numbers), which should include ATR%, ADX, RSI15, EMA flags, TL width, etc.

### 2.6 `surveillance.py` (post‑fill management)
- Controls SL tightening (BE+fees, abs‑$ lock, heatmap tighten, ratchet), TP extensions (with EPS/cooldown), status logging.  
- **Finding**: The loop is healthy after refactors; however, **if entries never happen**, we won’t see SL behavior. Prior `TP spam` is mitigated by tuple dedupe + cooldown. We also added pre‑TP1 absolute‑lock path with min‑change guard knobs for scalp.

### 2.7 `execution.py`
- Places bracket orders (entry/SL/TP RO legs) and mirrors TP replacements if enabled.
- No blocking issues observed in logs; earlier paper/live toggles respected.

---

## 3) Messaging contract (expected vs seen)

**Expected TG NO_TRADE example (pre‑refactor):**
```
🚫 NO TRADE — SOLUSD
Engine: trendscalp
Price: 202.5010
Reason: NO_EDGE
Levels: PDH 204.6310 | PDL 199.2140
Validators: ATRfloor 0.18% ≥ 0.12% ✓ | ADX 56.8≥20 ✓ | RSI15 37.0 ✓ | EMA200(5/15) aligned ✓ | Regime (TLwidth vs ATR) ✓
ATR14 0.3723 (0.18% ≥ 0.12%) | ADX 56.8≥20 | RSI15 37.0
EMA200(5m) 203.0180 | EMA200(15m) 203.0332 | TLw 0.7052 vs 0.42×ATR 0.1564
ma_long_ok ✗ | ma_short_ok ✓ | upper_break ✗ | lower_break ✗ | ema_up ✗ | ema_dn ✓
```

**Seen now:** `state.keys:` empty, `cfg.keys:` sometimes present but partial. → **The scanner or scheduler isn’t passing state.**

---

## 4) Telemetry & logs to watch
- `scan/NO_TRADE` events should include a JSON payload with `engine`, `pdh/pdl`, **plus** a `validators` sub‑struct or our `state` dict.
- The error `scalp ERROR unsupported operand type(s) for -: 'float' and 'NoneType'` ties to an indicator in TrendScalp math receiving `None` → check RSI/ADX/TL width readiness.

---

## 5) Findings & hypotheses (with checks)
1) **Missing `state` propagation**  
   - _Evidence_: TG debug shows empty `state.keys` despite cfg present.  
   - _Check_: Inspect `scheduler.py` code path invoking `messaging.no_trade_message(...)` and confirm args.
2) **`None` indicator during startup/backfill**  
   - _Evidence_: Type error in scalp error lines.  
   - _Check_: In `trendscalp.scan()`, guard any arithmetic: `if any(x is None for x in [adx, rsi15, ema200_5m, ema200_15m, atr14]): return NO_TRADE with reason=NOT_READY and state ready=False`.
3) **Late entry** comes from strict gate `TL break` + regime width + EMA alignment.  
   - _Action_: after restoring validator detail, tune knobs only with evidence from state values.

---

## 6) Instrumentation we added (temporary; remove later)
- In `trendscalp.scan()`: printed a **single‑line DEBUG TX** showing keys packaged for messaging.
- In `messaging.no_trade_message()`: printed **DEBUG RX** showing keys received (`meta/state/cfg`), plus `from:` tag with function name.
- These confirm the breakage point (producer vs consumer).

When fixed, remove this instrumentation and revert to clean logs.

---

## 7) Next actions (ordered, surgical)
1) **Scheduler**: Ensure we pass through `state` and `cfg` returned by `trendscalp.scan()` to `messaging.no_trade_message(...)`.
2) **TrendScalp**: Guarantee `state` is always populated when indicators are ready; otherwise, return `reason=NOT_READY` and `state.ready=False` (still include partial numbers if available, but never `None`).
3) **Indicators/Data**: On startup/backfill, provide a `min_bars_ready()` helper; callers branch until `True`.
4) **Re‑run**: Verify TG shows full validators block again. Only then revisit entry timing/knobs.
5) **Surveillance**: After entries resume, verify SL/TP trail messages and ensure pre‑TP1 abs‑lock operates with `TS_MIN_SL_CHANGE_ABS` guard to avoid SL churn.

---

## 8) Field dictionary (for consistency)
- **meta**: `engine, price, pdh, pdl, heatmap_levels_{5m,15m,1h,1d,30d}, trail_hints{use_vwap,avwap,pdh_pdl}, fee_pct, fee_pad_mult, min_sl_pct, max_sl_pct`.
- **state** (validators): `atr14, atr_pct, adx, rsi15, ema200_5m, ema200_15m, ema_up, ema_dn, tl_width, tl_atr_gate, ma_long_ok, ma_short_ok, upper_break, lower_break`.
- **cfg** (thresholds): `TS_VOL_FLOOR_PCT, TS_ADX_MIN, TS_TL_WIDTH_ATR_MULT, TP_R_MULTIS, SL_ATR_MULT, SL_NOISE_MULT, MIN_SL_PCT, MAX_SL_PCT` (only the ones used by TrendScalp messaging).

---

## 9) Runbook deltas (record for future)
- **Avoid Zones** are TASER‑only (for now). TrendScalp ignores them.
- **OI/CVD** integration planned; currently absent. Heatmaps present but not professional‑grade; not a blocker for validator messaging.
- **TrendFollow** is deprecated and should be fully removed from configs/knobs. Any lingering keys should be ignored.

---

## 10) Evidence snippets
- TG examples showing missing validators; scalp `NoneType` error lines; `scan/NO_TRADE` payloads carry `meta` only.

---

### End of document.
