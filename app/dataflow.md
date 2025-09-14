# Dataflow — **Summary Index** (v1)

> This file is a short, at‑a‑glance map of how data moves through the runtime. Detailed per‑file notes will live in `app/dataflow1.md`.

---

## 1) `app/trendscalp.py`
**Role:** *Signal construction* for TrendScalp. Turns OHLCV + indicators into either **NO_TRADE** meta/state or an **approved draft** (side, entry, SL, TPs, meta).

**Consumes:**
- `indicators.py` (EMA/RSI/ATR/ADX, VWAP/AVWAP if enabled)
- Heatmap levels from scan/meta (5m/15m/1h/1d/30d) via caller
- Config `C.*` knobs: TS_* gates, SL/TP sizing rails

**Produces:**
- `meta` (validators: ATRfloor, ADX, RSI15, EMA alignment, TL width vs ATR, regime)
- `state` (gate booleans, selected last values)
- Draft `{side, entry, sl, tps, meta}` when edge exists; otherwise **NO_TRADE** payload

**Emits:**
- `telemetry.log('scan', 'NO_TRADE'| 'APPROVED', ...)`
- (No direct messaging; caller forwards to `messaging.py`)

**Risks / Current Gaps:**
- **Root cause of missing validators in TG**: `state`/`cfg` sometimes **not populated** before calling messaging; only `meta` arrives ⇒ TG shows empty `state.keys`/`cfg.keys`.
- Must guard indicator series for leading `None` and choose last **non‑None** values; otherwise gates silently fail.
- Over‑patching risk: signal text building inside strategy can drift from `messaging.py` canonical format.

**Debug hooks now in use:** `_debug_inbound_from_manager`, `_debug_outbound_to_messaging` (print func name + keys).

**Next actions:** Ensure a single handoff struct `{meta,state,cfg}` is always assembled; centralize message formatting in `messaging.py` only.

---

## 2) `app/indicators.py`
**Role:** *Stateless math helpers* (EMA/RSI/ATR/ADX/etc.).

**Consumes:** OHLCV arrays.

**Produces:** Indicator arrays (or last‑value tuples for e.g., MACD).

**Emits:** None (pure library).

**Risks / Notes:**
- Returns contain leading `None` until warm‑up complete; callers must use last **valid** value.
- MACD returns **scalars for last bar**, not full series (callers often assume a list).

**Next actions:** None; contracts documented in `dataflow1.md`.

---

## 3) `app/data.py`
**Role:** *Data access layer*. Fetches/caches OHLCV for multiple timeframes; aligns series lengths.

**Consumes:** Exchange client / CCXT wrappers (via higher layer), config for lookbacks.

**Produces:** Dicts like `{open, high, low, close, volume, timestamp}` per TF with equal length lists.

**Emits:** `telemetry.log('data', ...)` on fetches (expected).

**Risks / Current Gaps:**
- If any TF returns misaligned lengths, downstream indicators mis-index and gates break quietly.
- Need explicit `assert/telemetry` for array alignment and recency.

**Next actions:** Add alignment and freshness checks; surface errors to caller.

---

## 4) `app/scheduler.py`
**Role:** *Orchestrator*. Periodically scans, calls engines (TrendScalp), forwards results to messaging or execution.

**Consumes:** `data.py` for OHLCV; `trendscalp.py` for signal; config & env; DRY_RUN flags.

**Produces:** Approved drafts to `execution.py` or NO_TRADE payloads to `messaging.py`.

**Emits:** `telemetry.log('run'|'scan'|'export', ...)` and TG messages via `messaging` helpers.

**Risks / Current Gaps:**
- If scheduler forwards only `meta` (omitting `state/cfg`), TG loses validator detail (observed today).
- Engine order toggles and DRY_RUN→paper/live logic must be consistent with `.env`.

**Next actions:** Enforce a single envelope `{engine, price, pdh/pdl, meta, state, cfg}` passed to `messaging.no_trade_message(...)`.

---

## 5) `app/messaging.py`
**Role:** *Presentation*. Renders Telegram strings for **NO_TRADE**, **APPROVED**, **MANAGE/STATUS**, etc.

**Consumes:** `{meta,state,cfg}` from engines/scheduler.

**Produces:** Human‑readable TG text; optional debug dumps of keys/values.

**Emits:** `tg_send(...)` and `telemetry.log('msg', ...)` if wired.

**Risks / Current Gaps:**
- Currently receives **meta only** sometimes ⇒ missing “Validators:” block in TG.
- Should not compute strategy values; just format what it’s given.

**Next actions:** Keep strict schema; if fields missing, print an explicit warning line (to catch regressions).

---

## 6) `app/surveillance.py`
**Role:** *Post‑entry manager*. SL/TP normalization, BE floor with fees, ATR‑aware trailing, heatmap‑tighten, trendline handler for TrendScalp; TP replace anti‑spam.

**Consumes:** Live price/TF OHLCV (1m/5m/15m/1h), draft meta, config (SL/TP rails, cooldowns).

**Produces:** Stop/TP amendments; close decisions (SL/TP/final/timeout); status telemetry.

**Emits:** `telemetry.log('manage', ...)`, `db.append_event(...)`, and TG updates.

**Risks / Current Gaps:**
- Can be *too chatty* on TP updates if de‑dupe gates loosened.
- Absolute lock `$` vs BE logic must not cross price; ensure min‑delta thresholds (`TS_MIN_SL_CHANGE_ABS`).
- Heatmap tighten depends on meta walls; if meta missing, tighten path is skipped silently.

**Next actions:** Keep locks in `locks.py` (fee‑padded BE, abs $), trail in `trail.py`, heatmap in `heatmap.py`; thin orchestrator only.

---

## 7) `app/execution.py`
**Role:** *Order placement*. Places entry + RO SL/TP brackets; mirrors TP changes from manager; DRY_RUN vs live handling.

**Consumes:** Drafts from scheduler/engine; `surveillance` decisions.

**Produces:** Orders (real or paper), order IDs, persistence; status messages.

**Emits:** `telemetry.log('exec', ...)`, `db.*` updates, TG confirms.

**Risks / Current Gaps:**
- DRY_RUN toggle must auto‑disable `PAPER_USE_START_BALANCE` for live (as agreed).
- Replace logic must cancel stale reduceOnly orders safely, with cooldowns.

**Next actions:** Verify free‑margin sizing when `DRY_RUN=false`; ensure paper/live flags are mutually exclusive.

### 7.1) `app/execution.py` — Learnings (from today’s review)
- **Role confirmation:** strictly post-approval; it never participates in NO_TRADE messaging. Therefore, the missing **Validators** block in TG is **not** caused by `execution.py`.
- **Inputs it expects:** an *approved* `draft` object with `{side, entry, sl, tps, meta}` plus runtime flags (`DRY_RUN`, paper/live). It does **not** expect or forward `state/cfg`.
- **Outputs:** order placement (real/paper), reduceOnly SL/TP brackets, and telemetry `exec/*` lines; it may echo select `meta` for auditing but does not construct validator text.
- **Important behavior we must preserve:**
  - When `DRY_RUN=false`, live sizing should use **free margin**, and `PAPER_USE_START_BALANCE` must be ignored/disabled.
  - SL/TP replacement logic already dedupes and cools down venue ops; we should not tighten those gates while fixing messaging.
  - Paper/live branches share the same logging surface, which is good for comparing behaviors.
- **Implication for our bug:** Since `execution.py` is downstream of approval, it cannot be the source of empty `state/cfg` in NO_TRADE messages. Root cause remains **scheduler ↔ trendscalp ↔ messaging** handoff.

### Bug-source summary (addendum)
- **Observed:** TG “NO_TRADE” shows `state.keys:` and `cfg.keys:` empty; `meta.keys:` present.
- **Confirmed exclusions:** `execution.py` is not in the NO_TRADE path; `surveillance.py` is post-entry only; `indicators.py` is pure math.
- **Most likely source:** Either:
  1) `trendscalp` returns early with only `meta` (and price/pdh/pdl) for NO_TRADE, skipping assembly of `state/cfg`, **or**
  2) `scheduler` builds the payload to `messaging.no_trade_message(...)` with only `meta` and omits `state/cfg`.
- **Why entries feel “late”:** Without gate-by-gate visibility, we can’t see *which* validator blocks progression; fixes target observability first, not thresholds.

### Non‑breaking fix plan (message path only)
1. **scheduler.py**
   - Always construct a full envelope:
     ```python
     payload = {
       "engine": "trendscalp",
       "price": price,
       "pdh": pdh, "pdl": pdl,
       "meta": meta or {},
       "state": state or {},
       "cfg": cfg or {},
     }
     ```
   - Pass it to `messaging.no_trade_message(payload)`.
   - If `state`/`cfg` are empty, log a one‑liner `telemetry.log('scan','NO_TRADE_WARN','missing state/cfg', {...})`.

2. **trendscalp.py**
   - Guarantee `(meta, state, cfg)` on **all** NO_TRADE returns.
   - `cfg` is a snapshot of key `C.TS_*` knobs (e.g., `TS_ADX_MIN`, `TS_TL_WIDTH_ATR_MULT`, `TS_VOL_FLOOR_PCT`).
   - Ensure last **valid** indicator values are used to build `state` (handle warm‑up `None`s).

3. **messaging.py**
   - Treat NO_TRADE input as a single `payload` dict; render “Validators” from `state` and “Config” from `cfg`.
   - If either dict empty, print a compact warning line so regressions are obvious.

4. **data.py** (hardening only)
   - Emit alignment/freshness checks so `trendscalp` doesn’t silently compute on stale/misaligned arrays.

### Files needed next (in order)
1. `app/scheduler.py` — to force the full NO_TRADE envelope.
2. `app/trendscalp.py` — to ensure `(meta, state, cfg)` are always returned on NO_TRADE and to snapshot `cfg`.
3. `app/messaging.py` — to verify the pure-presenter contract (no computation, just formatting).
4. *(Optional)* `app/data.py` — add alignment/freshness assertions.

---

## Cross‑cutting notes
- **Avoid Zones** are **TASER‑only**; TrendScalp currently does not use them (by design).
- OI/CVD/Flow: pending; TrendScalp currently relies on ATR/ADX/RSI/EMA/Regime + heatmap hints; wiring OI later.
- Telemetry timestamps are IST in the CSVs you shared.

## Today’s confirmed issue (root cause)
TG messages show:
```
state.keys: (empty)
cfg.keys:   (empty)
```
→ **Upstream is not populating/forwarding `state` & `cfg`** from TrendScalp to `messaging.no_trade_message`. Hotfix path: ensure `trendscalp.make_decision(...)` (or scheduler wrapper) constructs `{meta,state,cfg}` before calling messaging.

## What I still need to inspect in detail (will go into `dataflow1.md`)
- Exact function in `scheduler.py` that calls `messaging.no_trade_message(...)` and the exact payload keys.
- Exact function in `trendscalp.py` that assembles `meta/state/cfg` and where it might early‑return with only `meta`.
- Array alignment & recency guards in `data.py`.
