


# Trading Bot Runbook

**Today’s Delta — 16‑Sep (IST) — TrendScalp Chop‑Bleed Patch & TP1 Reliability**

- **Objective:** Stop chop bleed and ensure TP1 is captured more reliably by reshaping SL, adapting TP ladders, and tightening re‑entry hygiene. This update builds on earlier milestone flow.

- **.env changes applied:**

  *Risk & SL shape*
  - `SL_ATR_MULT=0.90` (was 1.25)
  - `SL_NOISE_MULT=2.10` (was 2.50)
  - `SL_MIN_GAP_PCT=0.0025` (was 0.0020)
  - `RATCHET_GRACE_SEC=150` (was 240)

  *TP logic*
  - `MODE_ADAPT_ENABLED=true` (on)
  - `TP_R_MULTIS=0.5,1.2,2.0` (was 0.6,1.6,2.6)
  - `TP_HIT_CONFIRM_BARS=1` (was 0)

  *Flow & trailing*
  - `FLOW_ENABLED=true` (on, with FLOW_REPLACE_TPS=true)
  - `TS_MILESTONE_MODE=true`
  - `TS_MS_STEP_R=0.5` (was 0.8)
  - `TS_MS_LOCK_DELTA_R=0.25` (was 0.20)

  *Filters / avoid chop*
  - `TS_USE_ADX_FILTER=true`
  - `TS_ADX_MIN=22` (was 24)
  - `DYN_AVOID_ENABLED=true`

  *Re‑entry hygiene*
  - `MIN_REENTRY_SECONDS=180` (was 120)
  - `BLOCK_REENTRY_PCT=0.0020` (was 0.0010)

- **Unchanged keys (kept for stability):**
  - `GLOBAL_NO_TRAIL_BEFORE_TP1=true`
  - `TP_LOCK_STYLE=to_tp1`
  - `LIVE_SIZING_USE_FREE_MARGIN=true`

- **Acceptance checklist (next 10–20 trades):**
  - ✅ Reduced chop bleed; fewer small‑loss churns
  - ✅ Higher TP1 hit‑rate in sideways markets
  - ✅ Cleaner milestone trail advancement, fewer micro SL updates
  - ✅ Re‑entries slower, with improved geometry

**Today’s Delta — 13‑Sep (IST) — Scheduler Fix for Re‑entry & SL Noise**

  - **Objective:** Prevent premature re‑entry blocking and bogus SL_PADDED logs when no trade exists.
  - **Code patches (applied):**
    - `scheduler.py`:
      - Re‑entry logic split into two stages:
        - **Pre‑draft:** only bar/time checks enforced with `side="NONE"`.
        - **Post‑draft:** price‑proximity guard applied with actual `draft.side`.
      - `_gate_reentry` now applies price‑proximity only when `side ∈ {LONG,SHORT}`.
    - `scheduler.py`:
      - SL/TP sanitizers run only when `side ∈ {LONG,SHORT}` and `entry,sl > 0`. Neutral drafts (`NONE`) are skipped.
  - **Impact:**
    - No more misleading `REENTRY_PRE price too close…` logs.
    - Same‑level flips (LONG→SHORT) are no longer pre‑blocked incorrectly.
    - `SL_PADDED` spam removed when there’s no active trade.
  - **Acceptance checklist (next 10–20 trades):**
    - ✅ `REENTRY_PRE` appears only for same‑bar/cooldown reasons.
    - ✅ `REENTRY_BLOCK` appears only when draft blocked for price‑proximity on same side.
    - ✅ `SL_PADDED` never logs with `entry=0.0`.
  
**Today’s Delta — 13‑Sep (IST) — Mode‑Adaptive TP Sizing (SOL stride)**
  - **Objective:** Make TP spacing reflect how SOL actually moves so we bank in chop and stretch in rallies, without code changes.
  - **Status:** Applied in `.env`. Base ladder switched to ATR; adaptive (chop vs rally) thresholds documented for wiring.

  - **Env changes (active now):**

```dotenv
# TP ladder grounded to SOL 5m ATR (chop‑friendly)
TP_MODE=atr
TP1_ATR_MULT=0.60
TP2_ATR_MULT=1.00
TP3_ATR_MULT=1.50
```

  - **Adaptive mode knobs (to widen in rallies; docs + thresholds):**

```dotenv
# Auto widen in rallies; keep tight in chop
MODE_ADAPT_ENABLED=true
MODE_CHOP_ATR_PCT_MAX=0.0025      # ≤0.25% of price → chop
MODE_CHOP_ADX_MAX=25               # ADX(14,5m) ≤ 25 → chop
MODE_CHOP_TP_ATR_MULTS=0.60,1.00,1.50
MODE_RALLY_TP_ATR_MULTS=0.90,1.60,2.60
```

  - **Flip & chop awareness (to turn -$110 into scratch/flip):**

```dotenv
FLIP_MIN_PEAK_R=0.30   # flip sooner if we’re on wrong side
FLIP_MAX_PER_TRADE=2   # allow a second corrective flip
DYN_AVOID_ENABLED=true # avoid buying tops / selling lows in ranges
```

  - **Why ATR ladder:** TP1 should sit near one normal SOL 5m impulse in chop. ATR reflects that stride without tying TP1 to deep structural SL (which inflated R and pushed TP1 unrealistically far).

  - **Quick sanity example (from telemetry discussion):**
    - If **price = 242** and **ATR(5m) ≈ 0.15%** → ATR ≈ 242×0.0015 = **0.363**.
    - With `TP1_ATR_MULT=0.60` → **TP1 distance ≈ 0.363×0.60 = 0.218**.
      - **Long TP1 ≈ 242 + 0.218 = 242.22**
      - **Short TP1 ≈ 242 − 0.218 = 241.78**

  - **Acceptance checklist (next 10–20 trades):**
    - ✅ TP1 hit‑rate ↑ in chop; far‑away TP1s disappear.
    - ✅ Earlier flips on wrong‑side entries (`FLIP_MIN_PEAK_R=0.30`) → fewer full -$110 stops.
    - ✅ In real rallies, FLOW extends TP2/TP3 (extend‑only); we still ride the leg.

  - **Rollback (TP only):**

```dotenv
TP_MODE=r
TS_TP_R=0.50,1.00,1.80
```
  - **Objective:** Stop taking marginal TrendScalp entries, reduce oversized stops, and add operational guards so managers exit cleanly and avoid order churn.
  - **Code patches (applied):**
    - `trendscalp_runner.py` — **Exit‑if‑flat** venue check (every `TS_CHECK_POS_EVERY_S` secs). If qty≈0 after partial TPs or external close → mark `CLOSED_FLAT`, TG a neutral exit, and stop the manager.
    - `trendscalp_runner.py` — **TP extend cooldown respected**: `_replace_takeprofits` now gated by `TP_EXTEND_COOLDOWN_SEC`; logs `TP_COOLDOWN_SKIP` if within window.
    - `trendscalp_runner.py` — **Optional TP hit confirmation**: `TP_HIT_CONFIRM_BARS` (default 0) requires N × 1m closes beyond TP1/TP2 to register hits.
    - `config.py` — parses `TP_HIT_CONFIRM_BARS`, `TS_CHECK_POS_EVERY_S` and prints a startup summary under `[CONFIG] TS_RUNTIME`.
  - **Env changes (applied in `.env`):**

```dotenv
# Confluence / gating
AGGRESSION=aggressive
TRENDSCALP_USE_AVOID_ZONES=true

# Risk rail & re‑entry hygiene
MIN_SL_PCT=0.0035
MIN_REENTRY_SECONDS=120
BLOCK_REENTRY_PCT=0.0050

# Bias & regime strictness
TS_MA_BUFFER_PCT=0.0005
TS_ADX_MIN=27
TS_OVERRIDE_EMA_RSI=false
TS_VOL_FLOOR_PCT=0.0025
TS_PULLBACK_PCT=0.0018
TS_ADAPT_REGIME=false
TS_TL_WIDTH_ATR_MULT=0.70

# New runtime knobs
TP_HIT_CONFIRM_BARS=0
TS_CHECK_POS_EVERY_S=10
```

  - **Rationale:**
    - **Fewer bad entries:** require clearer HTF/structure context (stricter MA buffer, higher ADX & vol floor, wider TL regime; avoid‑zones ON).
    - **Smaller typical loss:** reduce enforced SL rail from entry (0.35%), so a single stop doesn’t dwarf several wins.
    - **Operational safety:** managers stop when flat; TP order churn reduced by cooldown; optional TP close‑confirmation available.
  - **Acceptance checklist (next 4–6h):**
    - ✅ Fewer TrendScalp entries in mid‑thrust/chop; more `NO_EDGE` cards.
    - ✅ Median loss size per stop ↓ vs previous session (target: ≤ ~60–70 vs ~110).
    - ✅ TP updates respect cooldown; `TP_COOLDOWN_SKIP` appears (but not excessively).
    - ✅ No zombie managers — flat positions exit with `CLOSED_FLAT`.
  - **Rollback (single‑flip each if behavior worsens):**

```dotenv
AGGRESSION=conservative
MIN_SL_PCT=0.0060
MIN_REENTRY_SECONDS=90
BLOCK_REENTRY_PCT=0.0030
TRENDSCALP_USE_AVOID_ZONES=false
TS_MA_BUFFER_PCT=0.0010
TS_ADX_MIN=20
TS_OVERRIDE_EMA_RSI=true
TS_VOL_FLOOR_PCT=0.0012
TS_PULLBACK_PCT=0.0040
TS_ADAPT_REGIME=true
TS_TL_WIDTH_ATR_MULT=0.45
TP_HIT_CONFIRM_BARS=0
TS_CHECK_POS_EVERY_S=0
```

  - **Notes:**
    - This section is **additive** to the Milestone SL/TP Flow (13‑Sep) above. SL/TP messages and payloads are unchanged.
    - Keep `ENGINE_ORDER=trendscalp` during this test window to isolate effects.

**Today’s Delta — 13‑Sep (IST)**
  - **Objective:** Reduce green→red pre‑TP1 on shallow pokes and avoid late‑chase entries; block counter‑trend shorts.
  - **Code patches (already applied):**
    - `TS_PATCH_2025-09-12 ABS_LOCK_EVERY_TICK` — evaluate **absolute‑$ pre‑TP1 tighten every tick** (not only when `mg.changed==True`). Emits one‑shot `ABS_LOCK_WINDOW_MISSED` if threshold crossed but filtered by cooldown/min‑delta.
    - `TG_THROTTLE_10S` — per‑key (SL/TP/ext) Telegram throttling **10s** to cut bursts; does not change payloads.
  - **Env changes (applied in `.env`):**

```dotenv
SCALP_ABS_LOCK_USD=0.25        # pre‑TP1 safety tighten (catch shallower moves)
SL_TIGHTEN_COOLDOWN_SEC=45     # faster SL cadence
TS_MIN_SL_CHANGE_ABS=0.005     # allow smaller valid SL nudges
TS_PULLBACK_PCT=0.0040         # deeper pullback; fewer late chases
TS_MA_REQUIRE_15M=true         # block counter‑trend entries vs 15m bias
FLIP_MIN_PEAK_R=0.60           # PDE engages after 0.60R peak
```

  - **Rationale:**
    - Missed $‑lock windows on brief peaks (e.g., Trade 358) → run abs‑lock **every tick**; lower threshold to $0.25.
    - Cooldown/min‑delta skipped tightens near peak → reduce to 45s / 0.005.
    - Late entries during micro legs → require slightly **deeper pullback**; enforce 15m EMA alignment for side.
  - **Rollback (one‑flip each):**
    - Remove `ABS_LOCK_EVERY_TICK` block in `surveillance.py`.
    - In `.env`:

```dotenv
SCALP_ABS_LOCK_USD=0.30
SL_TIGHTEN_COOLDOWN_SEC=60
TS_MIN_SL_CHANGE_ABS=0.01
TS_PULLBACK_PCT=0.0030
TS_MA_REQUIRE_15M=false
FLIP_MIN_PEAK_R=0.80
```

  - **Audit checklist (14‑Sep, IST):**
    - ✅ Fewer trades with `mfe_px ≥ $0.25` that still exit at initial SL.
    - ✅ `ABS_LOCK_WINDOW_MISSED` appears rarely (≤10%).
    - ✅ Counter‑trend shorts blocked when 15m EMA opposes.
    - ✅ Telegram noise reduced (no repeated SL/TP spam within 10s windows).


**Today’s Delta — 12‑Sep (IST)**
  - **Objective:** Stop green→red leaks pre‑TP1 and ensure TrendScalp doesn’t miss near TP1 touches.
  - **Code patches (already applied):**
    - `TS_PATCH_2025-09-12` — **Do not push TP1 farther** for TrendScalp/Scalp during startup normalization; TP2/TP3 re‑ordered afterward.
    - `TS_FIX_2025-09-12` — **Distinct‑candle TP confirmation** (append closed 1m candles once per timestamp).
    - `TS_FIX_2025-09-12` — **Side‑correct BE** line (LONG: entry×(1+fees), SHORT: entry×(1−fees)).
    - `TS_PATCH_2025-09` — **Pre‑TP1 micro‑trail only after BE**; suppress misleading “no trailing” message when a guarded tighten was attempted but filtered by cooldown/min‑delta.
  - **Env changes (applied in `.env`):**

```dotenv
SCALP_ABS_LOCK_USD=0.30   # pre-TP1 safety tighten
TRENDSCALP_PAUSE_ABS_LOCKS=false  # allow abs-locks for TrendScalp
TS_MIN_SL_CHANGE_ABS=0.01  # let small valid SL nudges land
SL_TIGHTEN_COOLDOWN_SEC=60  # faster ratchet cadence
HEATMAP_TRAIL_GRACE_SEC=90  # earlier wall-based tighten
FLOW_BE_AT_R_PCT=0.50       # lock BE sooner (~0.5R or TP1)
```

  - **Rationale:**
    - Trade 356 missed TP1 due to startup sanitizer pushing TP1 from **240.011 → 240.2265**; patch caps TP1 for TrendScalp so we don’t miss fills.
    - Pre‑TP1 freeze caused stalls; abs‑lock + smaller SL min‑step + shorter cooldown let safe tightens land.
    - Distinct‑candle confirm prevents over/under‑counting the same 1m bar.

  - **Rollback (one‑flip each):**
    - Remove the `TS_PATCH_2025-09-12` block in `surveillance.py` to restore original TP1 normalization.
    - Revert BE line by restoring the previous `be_line` assignment (not recommended).
    - In `.env`:

```dotenv
SCALP_ABS_LOCK_USD=0
TRENDSCALP_PAUSE_ABS_LOCKS=true
TS_MIN_SL_CHANGE_ABS=0.02
SL_TIGHTEN_COOLDOWN_SEC=75
HEATMAP_TRAIL_GRACE_SEC=150
FLOW_BE_AT_R_PCT=0.75
```

  - **Audit checklist for tomorrow (13‑Sep, IST):**
    - ✅ % trades with **`hit_tp1=true`** increases vs 10‑Sep.
    - ✅ **TP1→loss leakage** drops (trades with `mfe_r ≥ 0.6` that close at `sl`).
    - ✅ Pre‑TP1 logs show **`*_ABS_LOCK_PRETP1`** or `*_TRAIL_SL` events when MFE ≥ $0.30.
    - ✅ No duplicate 1m bar entries in `TP_CONFIRM_CHECK`; each close appended once.
    - ✅ For shorts, BE announcements and pre‑TP1 micro‑trail only occur when **PnL@SL ≥ 0**.

---

**Today’s Delta — 10‑Sep (IST)**
  - **TrendScalp‑only** remains active.
  - **Adaptive Regime Multiplier** is now **enabled in code** (patch stamped 2025‑09‑10 08:34 IST in `trendscalp.py`).
    - Base `TS_TL_WIDTH_ATR_MULT` is still honored.
    - When **ADX ≥ `TS_ADAPT_ADX1` (default 30)** → effective multiplier relaxes to **`TS_ADAPT_MULT1` (default 0.35)**.
    - When **ADX ≥ `TS_ADAPT_ADX2` (default 40)** → effective multiplier relaxes further to **`TS_ADAPT_MULT2` (default 0.25)**.
    - The **effective** multiplier is exposed in telemetry/Telegram as `TS_TL_WIDTH_ATR_MULT_EFFECTIVE` for each decision window.
  - **Regime filter stays ON**; this only adapts the threshold under strong‑trend context.
  - **Telegram debug cleanup:** `DEBUG RX` block removed from `messaging.py` (no longer mirrored to telemetry or TG).

**Env Mirror — set these keys now (10‑Sep, IST)**
These keys drive the adaptive regime behavior (defaults shown). Adjust if needed.

```dotenv
# Adaptive regime control
TS_ADAPT_REGIME=true
TS_ADAPT_ADX1=30
TS_ADAPT_ADX2=40
TS_ADAPT_MULT1=0.35
TS_ADAPT_MULT2=0.25

# Legacy/base (still used below thresholds)
TS_TL_WIDTH_ATR_MULT=0.45
```

**Rationale**
- Clean breakouts were blocked when TL channel width < k×ATR despite strong trend (**ADX high** and **EMA alignment**). Adaptive multiplier lets high‑momentum legs pass without loosening the baseline in chop.

**Rollback / Safety**
- To disable adaptation quickly: `TS_ADAPT_REGIME=false`.
- To revert strict behavior without disabling adaptation entirely, set: `TS_ADAPT_MULT1=0.45`, `TS_ADAPT_MULT2=0.45`.

---


**Today’s Delta — 09‑Sep (IST)**
  - **TrendScalp‑only** remains active; TASER and TrendFollow are off.
  - **Regime filter ON**, **RSI filter OFF** (messages reflect toggles).
  - **Knob updates applied** (env):
    - `TS_VOL_FLOOR_PCT=0.0012` (0.12%) — skip ultra‑low energy entries.
    - `TS_TL_WIDTH_ATR_MULT=0.45` — avoid micro‑channel “regimes”.
    - `TS_MA_BUFFER_PCT=0.0010` — allow near‑EMA pulls (side‑aligned).
    - `BLOCK_REENTRY_PCT=0.0030` — small price‑distance guard on re‑entries.
    - `SL_ATR_MULT=1.25` — initial SL lifted out of routine 5m noise.
    - `TP1_LOCK_FRACR=0.40` — lock a bit more after TP1.
    - `FLOW_BE_AT_R_PCT=0.75` — earlier BE to reduce green→red.
  - **ADX gates** unchanged (strict/soft): 15 / 12 with EMA+RSI override enabled.
  - **Telegram cards**: Validators/CFG/STATE visible; debug stays ON until stable.

**Env Mirror — set these keys now (09‑Sep, IST)**
These reflect today’s operational state so .env / config.py stay in lockstep.

```dotenv
ENGINE_ORDER=trendscalp
TRENDSCALP_ONLY=true
TRENDSCALP_USE_AVOID_ZONES=false

# Pine-parity gates (with soft override wired)
TS_VOL_FLOOR_PCT=0.0012
TS_ADX_MIN=15
TS_ADX_SOFT=12
TS_REQUIRE_BOTH=false
TS_OVERRIDE_EMA_RSI=true
TS_USE_RSI_FILTER=false
TS_USE_REGIME_FILTER=true
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53
TS_RSI_OVERHEAT_HI=65
TS_RSI_OVERHEAT_LO=35
TS_TL_WIDTH_ATR_MULT=0.45
TS_MA_BUFFER_PCT=0.0010

# Trigger quality / momentum
TS_PULLBACK_PCT=0.0030
TS_WAI_MIN=0.75
TS_INTRABAR_ENTRY=true
TS_EARLY_SCALE_PCT=0.50
TS_VBREAK_ATR_HOT=0.25
TS_MAX_EXT_FROM_FASTEMA_ATR=1.2

# Exits / trailing guards
TS_EXIT_USE_CLOSE=true
TS_EXIT_CONFIRM_BARS=2
FLOW_BE_AT_R_PCT=0.75
TP1_LOCK_FRACR=0.40
TS_SL_MIN_STEP_ATR=0.05
TS_SL_MIN_BUFFER_ATR=0.15

# Reversal & re-entry hygiene
TS_REVERSAL_ADX_MIN=22
REENTRY_COOLDOWN_S=45
BLOCK_REENTRY_PCT=0.0030
NO_TRAIL_PRE_TP1=true
BE_AFTER_TP1=true
STALL_N_BARS=5

# Volatility-aware SL/TP
SL_ATR_MULT=1.25
SL_MIX_ALPHA=0.60
MIN_SL_PCT=0.0060

# Determinism during tuning
OPPORTUNISTIC_TWEAKS=false
```

**Delta — 09‑Sep (Evening, IST) — Applied & Rationale**
- **Vol floor 0.12%:** Losers clustered in ultra‑low energy; this skips weak setups without choking trends.
- **TL width × ATR = 0.45:** Prevents flip‑flop “regimes” inside micro channels.
- **MA buffer 0.10%:** Lets valid pullbacks pass near 200‑EMA when side‑aligned.
- **Re‑entry distance 0.30%:** Avoids same‑price churn after exits.
- **SL ATR 1.25× & TP1 lock 0.40:** Fewer noise stopouts; slightly stronger protection once TP1 is tagged.
- **BE at 0.75R:** Reduces green→red leakage without strangling winners.
- **Cleanup:** removed duplicate `TS_USE_REGIME_FILTER` key in `.env` (single source of truth).

## Previous Settings Snapshot (Sep 09, ~16:00 IST — Pre‑tweak)

```dotenv
# Engine
ENGINE_ORDER=trendscalp
TRENDSCALP_ONLY=true

# Gates (pre‑evening values)
TS_VOL_FLOOR_PCT=0.0010
TS_ADX_MIN=15
TS_ADX_SOFT=12
TS_REQUIRE_BOTH=false
TS_OVERRIDE_EMA_RSI=true
TS_USE_RSI_FILTER=false
TS_USE_REGIME_FILTER=true
TS_TL_WIDTH_ATR_MULT=0.40
TS_MA_BUFFER_PCT=0.0015
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53

# Triggers / momentum
TS_PULLBACK_PCT=0.0030
TS_WAI_MIN=0.75
TS_INTRABAR_ENTRY=true

# SL/TP & trailing (pre‑evening values)
SL_ATR_MULT=1.10
FLOW_BE_AT_R_PCT=0.75
TP1_LOCK_FRACR=0.35
BLOCK_REENTRY_PCT=0.0025
TS_SL_MIN_STEP_ATR=0.05
TS_SL_MIN_BUFFER_ATR=0.15
```

> This snapshot preserves the **exact values** before the latest env hardening (ATR floor 0.12%, TL width 0.45, SL 1.25×, TP1 lock 0.40, re‑entry 0.30%). Use it for quick diffs/rollbacks.

## Current TrendScalp Settings Snapshot (Sep 09, IST)

```dotenv
# Engine
ENGINE_ORDER=trendscalp
TRENDSCALP_ONLY=true

# Gates
TS_VOL_FLOOR_PCT=0.0012
TS_ADX_MIN=15
TS_ADX_SOFT=12
TS_REQUIRE_BOTH=false
TS_OVERRIDE_EMA_RSI=true
TS_USE_RSI_FILTER=false
TS_USE_REGIME_FILTER=true
TS_TL_WIDTH_ATR_MULT=0.45
TS_MA_BUFFER_PCT=0.0010
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53

# Triggers / momentum
TS_PULLBACK_PCT=0.0030
TS_WAI_MIN=0.75
TS_COOLDOWN_BARS=6

# SL/TP & trailing
SL_ATR_MULT=1.25
FLOW_BE_AT_R_PCT=0.75
TP1_LOCK_FRACR=0.40
TS_SL_MIN_STEP_ATR=0.05
TS_SL_MIN_BUFFER_ATR=0.15
```


**One‑flip Revert Switches (copy/paste if performance worsens)**

```dotenv
TS_OVERRIDE_EMA_RSI=false
TS_INTRABAR_ENTRY=false
TS_TL_WIDTH_ATR_MULT=0.45
TS_WAI_MIN=0.78
```

**Revert plan (flip these back if losses increase):**  
- Set `TS_OVERRIDE_EMA_RSI=false` (disable soft EMA+RSI override).  
- Set `TS_INTRABAR_ENTRY=false` (disable early scale‑in).  
- Raise `TS_TL_WIDTH_ATR_MULT` to `0.45`.  
- Raise `TS_WAI_MIN` to `0.78`.  
- Keep `TS_REQUIRE_BOTH=true` and strict `TS_ADX_MIN=20`.  
- Remove temporary debug from `trendscalp.py` and `messaging.py` (see “Temporary Debug Instrumentation” section) once cards look good.

This document captures the design, iteration plan, and audit metrics for TrendScalp (live) and TASER (parked). TrendFollow has been removed from runtime.

⸻

Engines & Priority
	1.	TrendScalp (ONLY ACTIVE ENGINE)
	•	Execution timeframe: 5-minute (all decisions from 5m closes; 1m is watch only).
	•	ML + Trendline composite: Lorentzian ANN bias AND (upper TL break or EMA trend) with momentum threshold (WAI).
	•	Early scale-in (intrabar) — ENABLED
	•	Purpose: reduce “late entry tax” during fast legs while keeping the structural discipline.
	•	Trigger (all must hold):
	•	EMA200(5m & 15m) aligned to side
	•	ADX(5m) ≥ soft gate (15) and rising vs last 4–6 bars
	•	RSI15 overheat: if RSI15 ≥ 65, require either
	•	Virtual break ≥ 0.25×ATR14(5m) beyond last TL (intrabar allowed), or
	•	Real TL break with intrabar distance ≥ 0.20×ATR
	•	Anti-chase: distance from 5m fast EMA ≤ 1.2×ATR14(5m)
	•	If RSI15 ≥ 70, tighten the virtual-break threshold to 0.35×ATR
	•	Action: open TS_EARLY_SCALE_PCT (default 0.50) of normal size intrabar; scale to full size on close-confirmed break (existing rule).
	•	Risk: SL identical to strict path; no absolute profit locks.
	•	Pine-parity filters (operational, no-code):
	•	15m RSI side-bias: RSI(15m) > 50 → LONG only; < 50 → SHORT only; 45–55 → NO TRADE.
	•	Volatility floor: ATR14(5m)/Price ≥ 0.20% (baseline; currently 0.12% during tuning).
	•	Regime width: TL channel width ≥ 0.5×ATR14(5m) (parity target 0.50×ATR; currently 0.42 during tuning).
	•	ADX gate: ADX(5m) ≥ 20.
	•	MA bias: price aligned with 200-EMA on 5m & 15m (LONG above, SHORT below).
	•	Avoid-zones: disabled for now (skip hidden blocks while tuning; re-enable after parity).
	•	Exits / Management (no-code):
	•	Close-confirmed trailing only: require 2 bar closes beyond TL (use 3 on high-vol days).
	•	Reversal guard: require ≥ 0.50R before any reverse.
	•	Reverse confirmation: opposite TL break must be confirmed by 2 closed 5m bars and be ≥ 0.25×ATR14 beyond the line.
	•	Reverse context: allow only if ADX(5m) ≥ 22 and price is on the correct side of 200-EMA(5m) for the new direction.
	•	No reverse in avoid-zones; apply cooldown = 6 bars and one-reverse-per-leg policy.
	•	Partial de-risk at TP1 (~0.8R) to prevent green→red even if core trails.
	•	Cooldown: ~6 bars (tunable 3–6) after any exit/reverse before new same-side entry.
	•	Slope & swings (parity with Pine/Lux): slope method = ATR, swing loopback = 14, slope multiplier = 1.4 (set in config).
	•	Two-stage absolute profit lock: PAUSED for TrendScalp (we will rely on close-confirmed TL trail + partials until refactor).
	2.	TASER (DISABLED for now)
	•	Kept idle while we align TrendScalp to Pine parity. Two-stage absolute profit lock remains configured but inactive due to engine disable.
	3.	TrendFollow (removed) — Engine is not used in live/runtime; code path retained only for archival backtests.

---

## Pine‑Parity Operating Rules (No Code Changes)

### Entry Gating (all must pass)  
1) **15m RSI side‑bias** (RSI&gt;50 only LONG, RSI&lt;50 only SHORT, 45–55 = NO TRADE)  
2) **ATR floor**: ATR14(5m)/Price ≥ **0.20%** *(baseline; **currently 0.12% during tuning**)*  
3) **ADX(5m) ≥ 20**  
4) **200‑EMA bias on 5m &amp; 15m** (side‑aligned)  
5) **TL channel width ≥ 0.5×ATR14** *(parity target 0.50×ATR; currently **0.42** during tuning)*  
6) **Avoid‑zones optional** — **currently disabled** while tuning.  
7) **Confluence**: Lorentzian bias **AND** (upper TL break **or** EMA trend) with **WAI ≥ 0.78**, `TS_REQUIRE_BOTH = true`
8) **RSI overheat guard**: when RSI15 ≥ 65 (long) or ≤ 35 (short), require structural confirmation (TL break or EMA momentum) even if bias is valid.
9) **Early scale‑in intrabar (enabled):** when the conditions above hold, allow a half‑size entry intrabar using the virtual‑break thresholds; full sizing remains gated by close‑confirmation.

### Exit / Management Policy  
- **Close‑confirmed TL trail**: `TS_EXIT_USE_CLOSE = true`, `TS_EXIT_CONFIRM_BARS = 2` (or 3 HV days)  
- **Reversal guard**: `TS_REVERSAL_MIN_R = 0.50`  
- **Partial de‑risk** at **TP1 (~0.8R)**; let core ride via TL trail  
- **Cooldown** after any exit/reverse: **~6 bars** (tunable **3–6**)  
- **Reverse confirmation:** opposite TL break confirmed by **2 closes** and **≥ 0.25×ATR14** beyond the line  
- **Reverse context:** **ADX(5m) ≥ 22** and **200-EMA(5m)** alignment for new side  
- **No reverse in avoid-zones**; apply **cooldown = 6 bars** and **one-reverse-per-leg**  
- **Note:** absolute $0.25/$0.50 locks are *paused for TrendScalp* (no‑code parity phase)

### Soft Override Path (captures early trend expansion) — *Wired and active*
The strict Pine path remains default. We now stage a conservative **soft override** so clean trend continuations can pass even when ADX is below the strict threshold. **Now wired in code and controlled by .env knobs.**

**Long soft gate:** if EMA200(5m & 15m) are **up** and **RSI15 > 55**, allow ADX ≥ **15** (strict gate stays at **20**).

**Short soft gate:** if EMA200(5m & 15m) are **down** and **RSI15 < 45**, allow ADX ≥ **15** (strict gate stays at **20**).

**Trigger integrity:** overrides still require a **structural trigger** — either a fresh **upper/lower TL break** *or* price already **≥ 1.0×ATR** beyond the last TL (treat as a **virtual break**) when EMAs and RSI agree. `TS_REQUIRE_BOTH=true` remains for the strict path; overrides act as exception gates only. Telemetry exposes hints via `adx_ok_strict` and `adx_ok_soft`.

#### RSI Cooling Reversal (experimental override — documented only)

Purpose: Catch high-quality **reversal shorts/longs** when RSI(15m) is **cooling from overheat** but EMA(15m) has not yet flipped.

**Activation criteria (all must hold):**
- **ADX ≥ 30** (strong context)
- **RSI(15m) cooling:** previously **≥ 65** and now **≤ 60** (falling slope)
- **Structural confirmation:** close **≥ 0.25×ATR14(5m)** **beyond** the relevant TL (lower for SHORT, upper for LONG)
- **Side bias:** prefer entries with price already on the correct side of **EMA200(5m)**; EMA200(15m) may still oppose (early reversal allowance)

**Notes:**
- This override is **separate** from the ADX soft gate; do **not** relax the ATR floor.
- Keep `TS_REQUIRE_BOTH=true` for the strict path; this override **adds** a controlled counter-trend entry only when cooling + strong break are present.
- **Telemetry:** until wired, monitor manually via cards; propose `rsi_cooling_override=true` once implemented.

## Reverse Minimization — Operational Policy (TrendScalp)

- **Minimum move:** require `TS_REVERSAL_MIN_R ≥ 0.50`.
- **Confirmation:** break opposite TL and confirm with **2 closed 5m bars**, and ensure break distance ≥ **0.25×ATR14**.
- **Context gates:** **ADX(5m) ≥ 22** and **200-EMA(5m)** agreement for the new side; **15m RSI** must favor the new side (outside 45–55).
- **Zone awareness:** **No reverses** inside avoid-zones (VWAP/cluster).
- **Hysteresis:** cooldown **6 bars** after a reverse; **one-reverse-per-leg**; optional cap **≤ 2 reverses/hour** per symbol (ops guard).

### Config to Set Now (no code)  
- `TS_EXIT_USE_CLOSE = true`  
- `TS_EXIT_CONFIRM_BARS = 2`  
- `TS_REQUIRE_BOTH = true`  
- `TS_WAI_MIN = 0.78` (currently 0.75 during tuning)  
- `TS_TREND_SLOPE_LEN = 30`  
- `TS_TREND_SLOPE_MIN = 0.02`  
- `TS_REVERSAL_MIN_R = 0.50`
- `TS_VOL_FLOOR_PCT = 0.0020` (baseline; **currently 0.0012 during tuning**)
- `TS_ADX_MIN = 20`
- `TS_ADX_SOFT = 15`  *(soft ADX gate when EMA+RSI agree)*
- `TS_OVERRIDE_EMA_RSI = true`  *(enable soft‑override path)*
- `TS_RSI_OVERHEAT_HI = 65`
- `TS_RSI_OVERHEAT_LO = 35`
- `TS_TL_WIDTH_ATR_MULT = 0.42`
- `TS_RSI15_NEUTRAL_LO = 47`, `TS_RSI15_NEUTRAL_HI = 53`
- `TRENDSCALP_PAUSE_ABS_LOCKS = true`
- `TS_BE_ARM_R = 0.5`, `TS_GIVEBACK_ARM_R = 1.2`, `TS_GIVEBACK_FRAC = 0.40`
- `TS_REVERSAL_ADX_MIN = 22`  
- `TRENDSCALP_USE_AVOID_ZONES = false` (temporarily off during parity tuning)
- `TS_INTRABAR_ENTRY=true`
- `TS_EARLY_SCALE_PCT=0.50`
- `TS_VBREAK_ATR_HOT=0.25`   # use 0.35 when RSI15 ≥ 70 (handled in code)
- `TS_MAX_EXT_FROM_FASTEMA_ATR=1.2`

(The above do not trigger without EMA alignment, ADX soft gate, RSI15 overheat, and the virtual‑break / anti‑chase checks.)

**Override policy (documentation only; no env keys yet):**
- **ADX override → ATR floor relax:** when **ADX ≥ 30** and EMA(5/15) slopes agree with side, treat **ATR floor as 0.0015 (0.15%)** for that decision window.
- **EMA+RSI override:** when EMA(5/15) slopes agree and **RSI15 > 55 (long) / < 45 (short)**, allow entry even if ML bias lags.
- Overrides still require a **structural trigger** (fresh TL break or **virtual break** ≥ 1×ATR beyond last TL).
- **RSI Cooling Reversal (documentation only; no env keys yet)**:
  - ADX ≥ 30
  - RSI15 cooling: was ≥65, now ≤60
  - Close ≥ 0.25×ATR beyond TL in trade direction

## Current TrendScalp Settings Snapshot (Sep 07, IST)

```
ENGINE_ORDER=trendscalp

# Pine-parity gates (current active)
TS_VOL_FLOOR_PCT=0.0012
TS_ADX_MIN=20
TS_ADX_SOFT=15
TS_OVERRIDE_EMA_RSI=true
TS_RSI_OVERHEAT_HI=65
TS_RSI_OVERHEAT_LO=35
TS_TL_WIDTH_ATR_MULT=0.42
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53
TS_REQUIRE_BOTH=true
TRENDSCALP_USE_AVOID_ZONES=false

# Trigger quality / momentum
TS_PULLBACK_PCT=0.0040
TS_WAI_MIN=0.75
TS_COOLDOWN_BARS=6

# Slope / reverse guards
TS_TREND_SLOPE_LEN=30
TS_TREND_SLOPE_MIN=0.02
TS_REVERSAL_ADX_MIN=22
TS_EXIT_USE_CLOSE=true
TS_EXIT_CONFIRM_BARS=2
# Noise-aware trailing (defaults in code; override via .env if needed)
TS_SL_MIN_STEP_ATR=0.05
TS_SL_MIN_BUFFER_ATR=0.15

# Determinism during tuning
OPPORTUNISTIC_TWEAKS=false
```

## Historical Settings Snapshots (Sep 06, IST)

### A) Baseline Strict (post‑revert)
```
ENGINE_ORDER=trendscalp

# Pine-parity gates (baseline strict)
TS_VOL_FLOOR_PCT=0.0020
TS_ADX_MIN=20
TS_TL_WIDTH_ATR_MULT=0.45
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53
TS_REQUIRE_BOTH=true
TRENDSCALP_USE_AVOID_ZONES=false

# Trigger quality / momentum
TS_PULLBACK_PCT=0.0040
TS_WAI_MIN=0.78
TS_COOLDOWN_BARS=6

# Slope / reverse guards
TS_TREND_SLOPE_LEN=30
TS_TREND_SLOPE_MIN=0.02
TS_REVERSAL_ADX_MIN=22
TS_EXIT_USE_CLOSE=true
TS_EXIT_CONFIRM_BARS=2

# Determinism during tuning
OPPORTUNISTIC_TWEAKS=false
```

### B) Forced‑Entry Loosen Test (short window)
```
ENGINE_ORDER=trendscalp

# Pine-parity gates (forced loosen)
TS_VOL_FLOOR_PCT=0.0010
TS_ADX_MIN=15
TS_TL_WIDTH_ATR_MULT=0.30
TS_RSI15_NEUTRAL_LO=48
TS_RSI15_NEUTRAL_HI=52
TS_REQUIRE_BOTH=false
TRENDSCALP_USE_AVOID_ZONES=false

# Trigger quality / momentum
TS_PULLBACK_PCT=0.0060
TS_WAI_MIN=0.55
TS_COOLDOWN_BARS=3

# Slope / reverse guards
TS_TREND_SLOPE_LEN=30
TS_TREND_SLOPE_MIN=0.015
TS_REVERSAL_ADX_MIN=18
TS_EXIT_USE_CLOSE=true
TS_EXIT_CONFIRM_BARS=2

# Determinism during tuning
OPPORTUNISTIC_TWEAKS=false
```

### C) Tighten Back (discipline restored)
```
ENGINE_ORDER=trendscalp

# Pine-parity gates (tighten back)
TS_VOL_FLOOR_PCT=0.0020
TS_ADX_MIN=20
TS_TL_WIDTH_ATR_MULT=0.45
TS_RSI15_NEUTRAL_LO=47
TS_RSI15_NEUTRAL_HI=53
TS_REQUIRE_BOTH=true
TRENDSCALP_USE_AVOID_ZONES=false

# Trigger quality / momentum
TS_PULLBACK_PCT=0.0040
TS_WAI_MIN=0.78
TS_COOLDOWN_BARS=6

# Slope / reverse guards
TS_TREND_SLOPE_LEN=30
TS_TREND_SLOPE_MIN=0.02
TS_REVERSAL_ADX_MIN=22
TS_EXIT_USE_CLOSE=true
TS_EXIT_CONFIRM_BARS=2

# Determinism during tuning
OPPORTUNISTIC_TWEAKS=false
```

## Tuning Log — Sep 06 (IST)

### 1) Baseline strict (post‑revert)
- VOL 0.0020, ADX 20, TLw×ATR 0.45, RSI15 band 47–53, REQUIRE_BOTH true,
  PULLBACK 0.0040, WAI 0.78, COOLDOWN 6, SLOPE_MIN 0.02, REVERSAL_ADX_MIN 22.
- Avoid‑zones remained OFF from earlier test (to avoid hidden blocks).

**Reason:** Align with Pine rules and stop chop entries; enforce side‑bias and structure.

### 2) Forced‑entry loosen test (short window)
- Avoid‑zones OFF, VOL 0.0010, ADX 15, TLw×ATR 0.30, RSI band 48–52,
  REQUIRE_BOTH false, PULLBACK 0.0060, WAI 0.55, COOLDOWN 3,
  SLOPE_MIN 0.015, REVERSAL_ADX_MIN 18.

**Outcome:** Immediate entries observed; confirmed plumbing & triggers.

### 3) Tighten back (confirm discipline)
- VOL 0.0020, ADX 20, TLw×ATR 0.45, RSI 47–53, REQUIRE_BOTH true,
  PULLBACK 0.0040, WAI 0.78, COOLDOWN 6, SLOPE_MIN 0.02, REVERSAL_ADX_MIN 22.

**Outcome:** Entries reduced; some borderline setups blocked.

### 4) Micro‑tuning pass (current)
- VOL **0.0018**, TLw×ATR **0.42**, WAI **0.75**, OPPORTUNISTIC_TWEAKS **false**.

**Reason:** Let borderline but directional structures through while keeping bias discipline.

### Monitoring & Acceptance (next 24h)  
- **Chop trades ↓ ≥40%** vs prior 24h  
- **Green→red flips rare**; median winner ≥ **1.2–1.5R**  
- **Reversal count/hour ↓ ≥30%**  
- Hit‑rate stable/↑; **positive expectancy** (small losers, fatter winners)

### 5) SL noise-aware trailing guards (implemented)
- Added ATR-scaled minimum SL step: require ≥ max(TS_MIN_SL_CHANGE_ABS, 0.05×ATR14) to move SL.
- Added minimum buffer from price: keep ≥ 0.15×ATR14 distance when tightening SL.

**Reason:** Prevent micro-ratchets into noise that convert green to red; keep trailing adaptive to volatility.

### 6) Missed‑trade capture plan (documented; no code yet)
- Add **Trend Override Path**: (a) **ADX>30** allows **ATR floor relax to 0.15%** with EMA slopes in‑side; (b) **EMA(5/15) slope + RSI15 (>55 long / <45 short)** can allow entries even if ML bias lags.
- Require a **structural trigger**: fresh TL break or **virtual break** ≥ 1×ATR beyond last TL when EMAs+RSI agree.
- Keep `TS_REQUIRE_BOTH=true` for strict path; overrides act as exception gates only.

**Reason:** Chart review showed clean bullish legs missed while ATR was slightly under 0.20% and ML bias lagged. Overrides capture strong, orderly trends without opening the door to chop.
---

## Risk / SL / TP Management

- **No trail before TP1**  
- **TP freeze when SL already < 0.7R from entry**  
- **MIN_SL_FRAC (preferred)**: e.g., `0.005` = 0.5%.  
- TP1/TP2/TP3 normalized with monotonic ordering and minimum R multiple.  
- Time-stop for scalp/trend engines (configurable).  
- Heatmap & progressive ratchet trailing applied with grace + cooldowns.  
- **TrendScalp Pine‑parity phase:**  
  - Two‑stage absolute profit lock **paused**; use **close‑confirmed TL trail** + **partial de‑risk at TP1** to prevent green→red.  
  - Maintain ATR/fee padding; no mid‑bar SL moves.

---

## Telemetry & Audit

Every trade now emits:

- `hit_tp1`, `hit_tp2`  
- MFE/MAE (px + R) tracked continuously  
- `TRADE_CLOSE_AUDIT` event at exit with full audit JSON:
  ```json
  {
    "engine": "trendscalp",
    "side": "LONG",
    "entry": 205.12,
    "exit": 206.44,
    "hit_tp1": true,
    "hit_tp2": false,
    "mfe_px": 2.1,
    "mae_px": 0.6,
    "mfe_r": 1.3,
    "mae_r": 0.4
  }
  ```
- `be_locked` (bool), `be_floor_px` (px actually enforced)  
- `tp_set_id` (monotonic int), `tp_changed` (bool), `tp_update_reason` ∈ {flow, line, stall, manual}  
- `reverse_confirmed_close` (bool), `atr_pad_used` (float)  
- `tp_freeze_active` (bool), `reentry_block_until` (ts)  
- `early_scale_in` (bool), `vbreak_atr` (float)
- `rsi_overheat` (bool), `adx_rising` (bool)

---

## Temporary Debug Instrumentation (REMOVE after fix)

**Purpose:** Trace what TrendScalp sends to `messaging.py` and what `messaging.py` receives to explain missing/blank validator lines in Telegram cards.

**What is enabled now (temporary):**
- `trendscalp.py` emits `scan/DEBUG_OUTBOUND` logs from **scalp_signal** and `scalp/DEBUG_OUTBOUND` from **scalp_manage** right before returning. The payload includes: `engine, price, pdh, pdl, validators_keys, state_keys, cfg_keys, upper_break, lower_break, ema_up, ema_dn, reason`, plus function name.
- `messaging.py` emits `msg/DEBUG_INBOUND` showing what it **actually received** from the engine (same key set), before formatting the Telegram text.

**How to read it quickly:**
1) For any `NO_TRADE` card that lacks Validators, find the nearest `scan/DEBUG_OUTBOUND` — confirm `validators_keys` is non‑empty.
2) Find the next `msg/DEBUG_INBOUND` — if keys disappeared, the drop is in the handoff; otherwise, it’s formatting.

**Removal checklist (execute after issue is fixed):**
1) Delete calls to `_debug_outbound_to_messaging(...)` in `trendscalp.py` (all return paths of `scalp_signal`, plus the end of `scalp_manage`).
2) Delete the helper `_debug_outbound_to_messaging` definition from `trendscalp.py`.
3) Delete the inbound debug block in `messaging.py` (the pre‑format snapshot/telemetry call).
4) Confirm `telemetry.csv` no longer emits `DEBUG_OUTBOUND` or `DEBUG_INBOUND` topics; keep normal `FILTER_BLOCK`/`STATUS`/`MANAGE`.

**Safety:** The debug is read‑only and has **no effect** on trade logic or orders. It will be fully removed once the Telegram formatter is verified.

---

Runtime Wiring Changes (Today)
	•	Scheduler now respects ENGINE_ORDER and is set to trendscalp only. TASER is disabled and will not be invoked.
	•	Startup heartbeat: on boot, the runtime emits STARTUP / ENGINE_ORDER telemetry with the normalized engine order.
	•	TrendScalp SL trailing guards: manager now enforces ATR-scaled minimum SL step and minimum buffer from price before committing SL moves; reasons are logged in manage audit (why[]).
	•	TrendScalp pre-gates (ATR/ADX/RSI15/EMA/Regime) emit FILTER_BLOCK telemetry when a setup is blocked (with detailed booleans/thresholds).
	•	Reverse audit: log_reverse() emits ALLOW/BLOCK with move_r, adx, ema200_ok, tl_confirm_bars, and ATR pad.
	•	Config knobs promoted to first-class in config.py and mirrored in .env.
	•	TrendScalp formatter compatibility: meta["filters"] and meta["validators"] now alias to filter_state to restore detailed Validator lines in Telegram.

Surveillance package structure (non-breaking refactor)

We are migrating app/surveillance.py into a package:
	•	app/surveillance/__init__.py – re-exports helpers
	•	utils.py – ATR/confirm/min-gap/formatters
	•	locks.py – fee-padded BE, absolute locks, guard_sl
	•	tp_orders.py – venue TP replacement + anti-spam
	•	heatmap.py – wall detect + tighten
	•	trail.py – trail after TP, progressive ratchet, TP order/sanitize, min-R enforcement
	•	status.py – status logging + close audit
	•	trend_handlers.py – TrendScalp wrapper for manager hooks
	•	core.py – houses surveil_loop; surveillance.py becomes a thin shim

Guarantee: drop-in, no behavior change. Surveil entry path remains app/surveillance.py.

---

## Unity Check (Sep 07, IST)

**Engines**
- `.env` → `ENGINE_ORDER=trendscalp` ✅
- `config.py` → `ENGINE_ORDER` default `"trendscalp"` ✅
- `scheduler.py` → uses `_engine_order()`; TrendScalp enabled by default ✅

**Trendlines parity**
- `.env` → `TS_TL_LOOKBACK=14`, `TS_TL_SLOPE_METHOD=atr`, `TS_TL_SLOPE_MULT=1.4` (default 1.6) ✅
- `config.py` → defaults set (**1.4**) ✅

- `.env` & `config.py` → `TS_VOL_FLOOR_PCT=0.0012`, `TS_ADX_MIN=20`, `TS_ADX_SOFT=15`, `TS_OVERRIDE_EMA_RSI=true`, `TS_TL_WIDTH_ATR_MULT=0.42`, `TS_RSI15_NEUTRAL_LO=47`, `TS_RSI15_NEUTRAL_HI=53`, `TRENDSCALP_USE_AVOID_ZONES=false` (currently off) ✅
- `trendscalp.py` → gates enforced; meta exposes `filter_cfg` + `filter_state` ✅

**Exits (no green→red)**
- `.env` & `config.py` → `TS_EXIT_USE_CLOSE=true`, `TS_EXIT_CONFIRM_BARS=2`, `TS_REVERSAL_MIN_R=0.50`, `TS_REVERSAL_ADX_MIN=22`, `TRENDSCALP_PAUSE_ABS_LOCKS=true`, `TS_BE_ARM_R=0.5`, `TS_GIVEBACK_ARM_R=1.2`, `TS_GIVEBACK_FRAC=0.40` ✅
- `trendscalp.py` → BE/give-back integrated; absolute locks guarded by pause flag ✅
- `trendscalp.py` → noise-aware trailing active: TS_SL_MIN_STEP_ATR and TS_SL_MIN_BUFFER_ATR enforced (defaults 0.05 / 0.15); overridable via .env ✅

**Reverse minimization**
- Runbook rules match code: 2-close TL confirm, ATR pad (~0.25×ATR), ADX≥22, 200-EMA alignment, cooldown 6 bars ✅

**Telemetry**
- `telemetry.py` → `TEL` constants; helpers `log_startup_engine_order`, `log_filter_block`, `log_reverse` ✅
- `scheduler.py` → emits startup engine order ✅
- `trendscalp.py` → emits filter blocks & reverse audits ✅
- TrendScalp telemetry exposes `TS_ADX_SOFT` / `TS_OVERRIDE_EMA_RSI` and now emits `adx_ok_strict` and `adx_ok_soft`.

**Docs**
- Runbook reflects TrendScalp-only, Pine-parity gates, tuned config, and reverse minimization; avoid-zones currently OFF ✅

---

## 24h Audit (IST) — Findings & Rationale

**Scope:** trades_2.csv, events_2.csv, telemetry_2.csv (since last push, ~11h IST)

### What we observed

- **TASER**: ~20 trades, net **−121.01**, avg **MFE_R ≈ 0.20**, **MAE_R ≈ 0.11** → conservative entries, not monetizing small edges.
- **TrendScalp**: ~10 trades, net **−42.31**, avg **MFE_R ≈ 0.84**, **MAE_R ≈ 0.22** → best intratrade run, biggest green→red leak.
- **Unmapped/legacy tag**: 118 trades, net **−571.56** → dashboard attribution bug (must ensure engine tag is carried across manage/close).

### Root causes (hypotheses confirmed in logs)
1) **No green→red guard:** trades reach 0.6–0.9R MFE then revert to ≤0; TP1 flags rarely recorded ⇒ we lack a giveback stop & explicit TP cross telemetry.  
   (Fixed) BE floor used inverted math for LONG/SHORT on fees; BE could still be below water. Corrected to LONG: entry*(1+fees), SHORT: entry*(1−fees).  

3) **Stall after impulse:** after a push to ≥0.8R, price stalls (no new HH/LL), then mean‑reverts; we leave profit on table.
4) **Attribution gaps:** some closes log without `engine` ⇒ PnL assigned to "unknown" in CSV.

---
## Critic Analysis — TrendScalp (SL & Re‑entry) [No‑code plan]

### What the engine already does well
- **Entry gating** is strong: ATR floor, ADX floor, RSI(15m) side‑bias, 200‑EMA(5m & 15m) alignment, regime width (TLw ≥ k×ATR), WAI momentum, pullback near fast EMA.
- **Management:** TL trailing, BE @ 0.5R, give‑back @ ≥1.0R (keep ~60%), reversal guard (needs ≥0.5R progress + context).

### Current weaknesses observed
- **Trailing commits immediately** (tick/price poke) → green can flip to red on noise; no **close‑confirm** or **time‑in‑zone** before SL is moved.
- **Re‑entry is purely filter‑driven** → can re‑fire at **same price** as exit when nothing new happened (e.g., Trade 308 → 309).
- **No same‑price churn guard** → exit 201.051 → re‑enter ~201.051.
- **No requirement that new entry improves geometry** (SL/TP asymmetry); can re‑enter with worse risk.

### Case study: 308 → 309 (short)
- 308: **SHORT 201.5600**, trailed into profit, closed **CLOSED_SL** at **201.0510**.
- 309: **SHORT 201.0510** opened almost immediately and later closed **red** at **201.7603**.
- Diagnosis: trailing likely committed on a wick; re‑entry happened without **fresh trigger** or **distance + pullback**; no same‑price guard.

### Refinement plan (no code yet)
1) **Noise‑aware trailing (shadow trail)**
   - Compute trail continuously, but **commit** only on **bar close beyond trail** or **time‑in‑zone** (10–15s) to avoid 1‑tick tags.
   - **Minimum SL step** scaled by **ATR** (e.g., 0.05–0.10×ATR) before moving SL.
   - Keep **BE @ 0.5R**, give‑back **40% after ≥1.0R**; no give‑back before 1.0R.

2) **Event‑driven re‑arm (soft cooldown, not time‑based)**
   Allow re‑entry **only if at least one** occurs after last exit:
   - **Fresh trigger**: new **TL break** (lower/upper as side appropriate) or **EMA re‑arm** (false→true since exit).
   - **Distance + pullback**: price extends ≥ **0.75×ATR** from last exit, then pulls back to ≤ **0.35×ATR** from fast EMA with **WAI ≥ 0.72**.
   - **Structure reset**: regime_ok turns **false→true** again (TL channel widens to threshold) after exit.

3) **No‑same‑price re‑entry radius**
   - Block re‑entry within **0.20×ATR(5m)** of the **last exit** price.

4) **Quality gate for re‑entries**
   - New SL must be **tighter** than SL at last exit by **≥ 0.15×ATR**.
   - Projected **TP1/SL R‑ratio ≥ 0.8R** at the prospective entry.

5) **Microstructure sanity**
   - **ΔADX(5m) ≥ 0** over last 4–6 bars at entry (trend strengthening).
   - **EMA200(5m) slope sign** consistent with side.
   - **Regime hysteresis**: once regime_ok turns **false**, require a **new TL anchor** and **true** again before entries.

### Why this beats a hard cooldown
- Re‑entry remains **fast** on genuine continuation (fresh break / extension+pullback), but avoids **same‑price churn** and **noise‑driven** re‑fires. No arbitrary time lockouts.

### Parameters to start with (tunable later)
- Close‑confirm or **10–15s** time‑in‑zone; **min SL step = 0.05–0.10×ATR**.
- No‑same‑price radius = **0.20×ATR**.
- Distance+pullback = **0.75×ATR** extension + **≤0.35×ATR** pullback + **WAI ≥ 0.72**.
- Improved SL geometry = **≥0.15×ATR** better than at last exit.
- Keep **BE 0.5R**, **give‑back 40%** after **1.0R**.

## Change Set (to implement now)

### A) TrendScalp Early Scale‑In (intrabar)
- Half‑size entry intrabar when EMA(5/15) aligned, ADX rising and ≥ soft gate, RSI15 ≥ 65, and either virtual break ≥ 0.25×ATR (≥ 0.35×ATR if RSI15 ≥ 70) or real break with 0.20×ATR intrabar distance.
- Anti‑chase: distance from fast EMA ≤ 1.2×ATR.
- Scale to full size on the existing close‑confirmed break rule; management unchanged (no absolute $ locks).



### D) Re‑entry Cooldown & Single‑engine ownership
- After any close/reverse on a symbol, block new entries for `REENTRY_COOLDOWN_S` (30–60s) and ensure only the active engine can re‑engage.

### E) Attribution Fix
- Thread `engine_tag` through **approve → place → manage → close**; default to last known engine if missing at close.




### H) Global TP/SL De-dup & Reverse-at-0 Guard
- Extended TP/SL spam suppression (de-duplication, Telegram throttling) to all active engines (TrendScalp + TASER).
Reverse-at-0 guard implemented: re-entry/reverse is blocked within max(TP_EPS, 0.20×ATR) around the prior exit to prevent 0-PnL churn.

---

## Config Knobs (add/update in `.env` and `config.py`)

Keep knobs minimal — avoid proliferation. 	•	Global:
NO_TRAIL_PRE_TP1=true
BE_AFTER_TP1=true
STALL_N_BARS=5
REENTRY_COOLDOWN_S=45
TS_MIN_SL_CHANGE_ABS=0.01
	•	TrendScalp trailing guards:
TS_SL_MIN_STEP_ATR=0.05
TS_SL_MIN_BUFFER_ATR=0.15
	•	TP noise tolerance / TrendScalp virtual-break thresholds
TP_EPS=0.01
TS_VBREAK_ATR_NORM=0.25
	•	Scalp:
SCALP_ABS_LOCK_USD=0.50 (Stage 2). Stage 1 ($0.25) is internal default; enabled.
	•	TASER:
TASER_ABS_LOCK_USD=0.50 (Stage 2). Stage 1 ($0.25) internal; enabled.

(TrendFollow knobs removed.)

(Internal constants; do not put in .env unless asked)  
- OHLCV source fallback + backfill are handled inside `fetcher.py`.  
- Telemetry verbosity in DRY_RUN is enabled by default.

---

## Telemetry Additions (one‑shot per trade unless value changes)

Emit on manage/close:
- `tp1_crossed`, `tp2_crossed`, `tp3_crossed` (booleans)
- `mfe_px`, `mae_px`, `mfe_r`, `mae_r`
- `extreme_px` (max for long, min for short since entry)
- `giveback_stop_px` (when armed)
- `exit_reason ∈ {tp, sl, giveback, stall, reverse}`

This enables **TP1→loss leakage** analytics and dashboard heat‑maps.

---

## Dashboard / CSV

- Engine 24h & 7d toggles with per‑exchange split (Delta live; Dhan later).
- Export: `runtime/engine_summary_24h.csv` and `..._7d.csv`.
- Surface **TP1→loss %**, **avg MFE_R**, **avg MAE_R**, and **Giveback‑armed %**.
- Note: `ENGINE_SPLIT_CSV` export is triggered on a scheduler timer (15m) even if the dashboard is closed; that’s why you see export lines in logs.

---

## Message Rate Limits & Anti‑Spam — implemented

	•	Deduplicate identical TP updates and SL updates within short windows.
	•	Coalesce repeated “NO TRAIL PRE TP1” notices to one per position lifecycle.
	•	Telegram: throttle identical messages to avoid bursts during sideways periods.
	•	Applied globally across TrendScalp and TASER (TrendFollow removed).


## Tomorrow’s Checklist

1) Verify engine attribution (no `unknown` rows).  
2) Green→red: % trades with `giveback` exit vs `sl` once armed.  
3) TF reversals: count flip‑flops pre/post ATR‑padded close rule.  
4) Net PnL vs. yesterday and **TP1→loss leakage** reduction.  
5) Review outliers with high `mfe_r` but `exit_reason != tp`.

---

## Daily Audit Loop

	•	Keep the same structure; all TrendFollow-specific items removed.
	•	Reverse-at-0 guard status updated to implemented.

---

## Daily Delta (Today vs Yesterday)

 
- Added robust TP parsing and fallback.  
- Forced engine attribution consistency in manage/close logs.  
- Extended .env/config with OHLCV sourcing knobs and telemetry flags.  
- Verified no duplicate env keys.  
- Applied global TP/SL de-duplication to reduce 1000+ spam messages in TrendScalp.  
- Rolled out global TP/SL spam suppression to TrendScalp; introduced optional $0.50 abs lock knobs for Scalp.
- Reverse-at-0 issue identified in TrendScalp/TrendFollow; guard to be implemented next.  


---

## Watchlist (Next Hour)

**Tripwires for a clean TrendScalp entry**
- **ATR14(5m)/Price ≥ 0.12%** (current floor while tuning)
- **RSI(15m) leaves 45–55**
  - LONG: **>55** (avoid overheat unless structural trigger)
  - SHORT: **<45**
- **EMA200 alignment (5m & 15m)**
  - LONG: price above both; SHORT: price below both
- **Structural trigger**
  - TL break in trade direction, or **virtual break ≥ 1×ATR** beyond last TL when EMAs+RSI agree
  - **Cooling override:** if RSI15 cooled from ≥65 → ≤60 and ADX ≥30, require close ≥ 0.25×ATR beyond TL
- **ADX context**
  - Strict: **ADX ≥ 20**; Soft: **ADX ≥ 15** with EMA+RSI agreement

**Telemetry flags to watch in cards/logs**
- `adx_ok_strict` / `adx_ok_soft`
- `rsi_overheat_long` / `rsi_overheat_short`
- `ma_long_ok` / `ma_short_ok`, `upper_break` / `lower_break`
- `regime_ok` (TL width ≥ k×ATR)

## Iteration Plan

- **Phase 0 (06‑Sep): Pine‑parity operational mode — TrendScalp only; locks paused for Scalp; enforce 15m RSI + ATR/ADX/Regime/200‑EMA gates; exits via close‑confirmed TL trail; reversal guard + cooldown.
- **Days 1–2**: Fix obvious leaks (SL too tight, premature reversals).  
- Re-evaluate $0.50 absolute profit lock for TrendScalp after Pine-parity stabilization; keep paused until then.
- **Days 3–5**: Use audit metrics to tune ATR multipliers, cooldowns, confirm-bar logic.  
- **Days 5–7**: Freeze hybrid engine config once stable across market regimes.  
- Beyond: scale to multiple pairs (SOL today, others tomorrow), multiple exchanges (Delta, Dhan).

---

## Operator Notes

- Always keep `.env` and `config.py` toggles consistent (`DRY_RUN`, `TREND*ENABLED`, SL/TP knobs).  
- Preferred key: `MIN_SL_FRAC` for unambiguous SL floor.  
- One trade at a time; engine priority enforced in `scheduler.py`.  
- When `DRY_RUN=false`, `PAPER_USE_START_BALANCE` is auto-disabled so live sizing uses free margin.  
- Absolute $ profit locks remain **disabled**; TrendScalp uses close‑confirmed TL trail + partial at TP1.

- Temporary DEBUG is enabled (see **Temporary Debug Instrumentation**). **Remove it** once Telegram cards show Validators/ADX/RSI/EMA lines again.

---

## Next Steps

- Once validator lines in Telegram are restored, **strip the temporary debug hooks** from `trendscalp.py` and `messaging.py` per the removal checklist.
- User will provide logs daily (trades, events, telemetry).  
- Assistant will audit, propose refinements, and patch incrementally.  
- Goal: a **profitable, robust, no-leak bot** that extracts PnL across all scenarios.  
- Monitor effectiveness of abs lock in Scalp once enabled.
---

## Today’s Delta — 13‑Sep (IST) — **Milestone SL/TP Flow (TrendScalp)**

**Objective:** Convert many small scratches into fewer, larger winners while capping losers. Avoid green→red after TP1.

**Code changes (applied):**
- `app/runners/trendscalp_runner.py`: Added **milestone-based SL** with fallback to FSM.
  - Pre‑TP1: no micro trailing; optional **ABS lock** once `mfe_abs ≥ SCALP_ABS_LOCK_USD`.
  - After TP1: **lock BE+fees**, then ratchet only on **milestones**: every `TS_MS_STEP_R` gained beyond TP1 raises SL by `TS_MS_LOCK_DELTA_R` (in R) from entry.
  - After TP2: jump SL to `TS_TP2_LOCK_FRACR × (entry→TP2)` and **trail by ATR** (`TS_POST_TP2_ATR_MULT`).
  - Tighten‑only, cooldowns & venue anti‑churn preserved. Telemetry now emits `MS_MODE` on manage start.
- `app/scheduler.py`: Recovery **routes by engine** using `db.get_trade_engine(...)` (no more TASER-only resume).
- `app/surveillance.py`: **Exit‑if‑flat** guard in `surveil_loop` (close if qty≈0 after TP fills).

**Env (add/update):**
```dotenv
# ——— Milestone manager (TrendScalp)
TS_MILESTONE_MODE=true          # set false to instantly revert to previous FSM SL behavior
TS_MS_STEP_R=0.5                # milestone every +0.5R after TP1
TS_MS_LOCK_DELTA_R=0.25         # each milestone raises SL by +0.25R from entry
TS_TP2_LOCK_FRACR=0.70          # on TP2, SL jumps to 70% of entry→TP2
TS_POST_TP2_ATR_MULT=0.50       # post‑TP2 ATR trail multiplier

# ——— Cadence & protection (recommended with milestone)
SL_TIGHTEN_COOLDOWN_SEC=60      # commit SL at most every 60s
SL_MIN_GAP_ATR_MULT=0.30        # keep SL ≥0.30×ATR from price pre‑TP1
SCALP_ABS_LOCK_USD=0.20         # optional pre‑TP1 BE insurance after small MFE
LOCK_NEVER_WORSE_THAN_BE=true   # once TP1 hits, worst case = breakeven (fees padded)
```

**Operational notes:**
- Messages/payloads unchanged; milestone mode only alters **when** SL tightens.
- TP extensions remain **extend‑only** and respect existing `TP_MIN_INTERVAL_S`/`TP_EPS` guards.
- Telegram throttle (`_tg_send_throttled`) remains 10s per key; can tune later.

**Acceptance checklist (next 4–6 hours):**
- ✅ **Loser size ↓**: typical losing trade risk ≤ previous (target ≤ ~60–70 vs ~110).
- ✅ **TP1→loss leakage ↓**: trades with `mfe_r ≥ 0.6` that close at SL fall by ≥30%.
- ✅ **Winners fatter**: median winner ≥ **1.2–1.5R**; more TP2/TP3 prints.
- ✅ **Telegram** shows: `TP1 HIT → BE LOCKED`, milestone ratchets (fewer micro SL updates).

**Rollback (instant):**
- Set `TS_MILESTONE_MODE=false` in `.env` and restart. This restores the prior FSM‑only SL proposals; TP logic unchanged.

**Diagnostics to watch (telemetry.csv):**
- `MS_MODE` line on manage start with `stepR` / `lockDeltaR` values.
- `manage/STATUS` → `mfe_px`, `mae_px`, `hit_tp1`, `hit_tp2`.
- `SL_COOLDOWN_SKIP` messages should be **rarer** than before.
- Exit audit lines (`CLOSED_*`) carry `engine: trendscalp` (resume routing fix).

**Next tuning levers (if needed):**
- If exits are still too small: increase `TS_MS_LOCK_DELTA_R` to **0.30** or raise `SL_TIGHTEN_COOLDOWN_SEC` to **90**.
- If give‑back feels high post‑TP2: raise `TS_TP2_LOCK_FRACR` to **0.80** or `TS_POST_TP2_ATR_MULT` to **0.60**.
- If reaching TP1 is still hard: lower `SCALP_ABS_LOCK_USD` to **0.15** and/or `SL_MIN_GAP_ATR_MULT` to **0.25**.

---

## Appendix — Revert vs Optimized Settings (13‑Sep IST)

### Context
You reverted the latest knob changes. This appendix captures a **clean comparison** between the **Last Known Working (LKW)** settings, the **Reverted** baseline you’re on now, and the **Optimized** set that balances *not missing rallies* with *not placing TP1 too far*.

### Objectives
1) **Never miss rallies** → allow progressive TP widening **after** momentum confirms.  
2) **TP1 not far** → base TP1 on 5m ATR so it’s reachable in chop; avoid green→red.  
3) **No pre‑TP1 SL choke** → no aggressive locks before TP1; enforce real min gap from price.  
4) **Fewer same‑price churns** → sane re‑entry radius & fresh‑trigger requirement.

### Side‑by‑Side Knob Comparison

| Area | LKW (earlier) | Reverted (now) | **Optimized (proposed)** |
|---|---|---|---|
| TP mode | `r` (R‑based ladder) | `r` | **`atr`** (ladder tied to SOL stride) |
| TP1 distance | `TS_TP_R=0.50` (implied) | `0.50` | **`TP1_ATR_MULT=0.60`** (≈ one 5m impulse) |
| TP2/TP3 | R‑multiples | R‑multiples | **`TP2_ATR_MULT=1.00`, `TP3_ATR_MULT=1.50`** (extend‑only later widen) |
| Adaptive widen | off | off | **`MODE_ADAPT_ENABLED=true`**; **chop:** `0.60,1.00,1.50` → **rally:** `0.90,1.60,2.60` |
| Pre‑TP1 locks | sometimes on | on (caused SL jumps) | **off**: `TRENDSCALP_PAUSE_ABS_LOCKS=true` |
| BE arm (flow) | 0.75R | 0.50R | **0.80R**: `FLOW_BE_AT_R_PCT=0.80`, **TS_BE_ARM_R=0.60** |
| SL min gap (ATR) | 0.30×ATR | 0.30×ATR | **0.50×ATR** + **`SL_MIN_GAP_PCT=0.0016`** floor |
| SL buffer (ATR) | 0.15×ATR | 0.15×ATR | **`TS_SL_MIN_BUFFER_ATR=0.25`** |
| SL grace/cadence | 3s / 150s | 3s / 150s | **`TP_LOCK_GRACE_SEC=20`**, **`RATCHET_GRACE_SEC=240`** |
| Milestone trail | off | off | **on**: `TS_MILESTONE_MODE=true`, `TS_MS_STEP_R=0.8`, `TS_MS_LOCK_DELTA_R=0.20` |
| TP2 lock & trail | N/A | N/A | **`TS_TP2_LOCK_FRACR=0.70`, `TS_POST_TP2_ATR_MULT=0.50`** |
| Re‑entry hygiene | light | light | **`BLOCK_REENTRY_PCT=0.0050`**, **REENTRY_COOLDOWN_S=45**, fresh‑trigger required (doc) |

> **Interpretation:** The Optimized set makes **TP1 realistically close** in chop (ATR‑based), **keeps SL from jumping** before TP1, and only widens TP2/TP3 when momentum supports a rally, so you can ride legs without giving back early.

### What We Improved vs Reverted
- **TP realism:** TP1 is no longer tied to deep structural SL (which inflated R and pushed TP1 far). ATR‑based TP1 ≈ one normal 5m stride → **higher TP1 hit‑rate** and fewer green→red flips.
- **Rally participation:** Adaptive widen shifts TP2/TP3 **only in rallies** (ADX + ATR pace), so we **don’t cap** winners in trends but **don’t over‑reach** in chop.
- **Pre‑TP1 stability:** Pausing abs‑locks + enforcing **0.50×ATR + 0.16%** minimum SL gap **prevents micro‑closes** under price.
- **Smoother trail:** Grace (20s) + slower ratchet (240s) **reduces SL thrash**, yet milestone trail still advances once gains are real.

### Exact Optimized ENV Block (copy/paste)
```dotenv
# TP ladder grounded to SOL 5m ATR (chop‑friendly)
TP_MODE=atr
TP1_ATR_MULT=0.60
TP2_ATR_MULT=1.00
TP3_ATR_MULT=1.50

# Auto widen in rallies; keep tight in chop
MODE_ADAPT_ENABLED=true
MODE_CHOP_ATR_PCT_MAX=0.0025
MODE_CHOP_ADX_MAX=25
MODE_CHOP_TP_ATR_MULTS=0.60,1.00,1.50
MODE_RALLY_TP_ATR_MULTS=0.90,1.60,2.60

# Pre‑TP1 protection (no choke)
TRENDSCALP_PAUSE_ABS_LOCKS=true
FLOW_BE_AT_R_PCT=0.80
TS_BE_ARM_R=0.60
TP_LOCK_GRACE_SEC=20
RATCHET_GRACE_SEC=240
SL_MIN_GAP_ATR_MULT=0.50
SL_MIN_GAP_PCT=0.0016
TS_SL_MIN_BUFFER_ATR=0.25

# Milestone trail (post‑TP1)
TS_MILESTONE_MODE=true
TS_MS_STEP_R=0.8
TS_MS_LOCK_DELTA_R=0.20
TS_TP2_LOCK_FRACR=0.70
TS_POST_TP2_ATR_MULT=0.50

# Re‑entry hygiene
REENTRY_COOLDOWN_S=45
BLOCK_REENTRY_PCT=0.0050
```

### How This Meets Your Goals
1) **“Never miss rallies”** – TP2/TP3 widen only when ADX/ATR support it; SL milestones trail behind **confirmed** gains (not micro‑pokes).  
2) **“TP1 not far”** – TP1 is ≈ one 5m stride; realistic in chop so winners aren’t forfeited.  
3) **“Don’t go red after green”** – No pre‑TP1 SL choke, later BE arm, and a real min gap from price.

### What to Watch (next 10–20 trades)
- **TP1 hit‑rate** ↑ vs reverted.  
- **TP1→SL leakage** ↓ ≥ 30%.  
- **Median winner** ≥ 1.2–1.5R; **no instant SL yank** pre‑TP1 in logs.  
- Telegram should show fewer SL updates pre‑TP1; milestone updates post‑TP1.

---

If you want, we can stage this in two steps: enable **TP_MODE=atr** first; then add **milestone trail** after confirming TP1 realism.

## Redbook — 14‑Sep‑2025

**Time:** 14‑Sep‑2025, ~21:45 IST  
**Purpose:** Reduce over‑filtering and allow TrendScalp to participate in clean rallies while still skipping dead tape.

**Env Changes:**
- `TS_VOL_FLOOR_PCT`: 0.0025 → 0.0015 (lowered volatility floor to 0.15% of price)  
- `TS_ADX_MIN`: 27 → 22 (reduced strict ADX requirement to let more legs through)  
- `TS_USE_REGIME_FILTER`: true → false (disabled TL‑width regime filter to stop blocking strong expansions)

### Addendum — 14‑Sep‑2025 11:15 IST — Minimalist Entry & Management Baseline

**Purpose:** Freeze a simplified baseline (looser entry, no adaptive/flow/milestone logic) for fast iteration and to isolate entry edge vs. management. Mirrors the exact .env block pushed just now.

**ENV (active):**
```dotenv
# ENTRY
TS_USE_RSI_FILTER=false
TS_USE_REGIME_FILTER=false
TS_REQUIRE_BOTH=false
TS_EMA_FILTER=false
TS_SMA_FILTER=false
TS_ADX_MIN=24
TS_ADX_SOFT=22

# STOPS (keep current blended SL + min/max)
SL_MIX_ALPHA=0.60
SL_ATR_MULT=1.25
SL_NOISE_MULT=2.50
MIN_SL_PCT=0.0035
MAX_SL_PCT=0.0130

# TAKE PROFITS — simple R only
TP_MODE=r
TP_R_MULTIS=0.6,1.6,2.6

# MANAGEMENT — ONLY these two actions
GLOBAL_NO_TRAIL_BEFORE_TP1=true
TP_LOCK_STYLE=to_tp1
TP_HIT_CONFIRM_BARS=0
FLOW_ENABLED=false
MODE_ADAPT_ENABLED=false
TS_MILESTONE_MODE=false
FLIP_ENABLED=false
DYN_AVOID_ENABLED=false
STAGED_LOCKS_ENABLED=false
OPPORTUNISTIC_TWEAKS=false

# COOL-OFF + hygiene (keep)
MIN_REENTRY_SECONDS=120
REQUIRE_NEW_BAR=true
```

-**Notes:**
- Entry gates loosened: regime/RSI/EMA/SMA filters OFF; soft ADX 22, strict 24; `TS_REQUIRE_BOTH=false`.
- Management intentionally minimal: no flow/milestone/adapt/flip/dyn-avoid/staged locks; only `TP_LOCK_STYLE=to_tp1` with `GLOBAL_NO_TRAIL_BEFORE_TP1=true`.
- Keep cool‑off hygiene: `MIN_REENTRY_SECONDS=120`, `REQUIRE_NEW_BAR=true`.
- This addendum supersedes earlier 14‑Sep env deltas for the current test window; prior values remain documented above for rollback.

```

### 🔄 Rollback Snapshot — before 14‑Sep‑2025 11:15 IST patch

This snapshot captures the key env knobs **prior** to the minimalist baseline applied at 11:15 IST, so we can revert quickly if needed.

| Key | Previous Value |
|---|---|
| SL_ATR_MULT | 0.9 |
| SL_NOISE_MULT | 2.1 |
| MIN_SL_PCT | 0.0050 |
| MAX_SL_PCT | 0.0115 |
| TP_MODE | atr |
| TP_R_MULTIS | 1.0,1.8,2.8 |
| FLOW_ENABLED | true |
| MODE_ADAPT_ENABLED | true |
| TS_MILESTONE_MODE | true |
| FLIP_ENABLED | true |
| DYN_AVOID_ENABLED | true |
| STAGED_LOCKS_ENABLED | true |
| OPPORTUNISTIC_TWEAKS | true |

> Use this table to restore the pre‑patch configuration. Keep it adjacent to the 14‑Sep‑2025 entry for easy comparison.
```

---
## 2025-09-16 14:20 IST — TrendScalp Regime Config Promotion

**Objective:**
- Elevate regime logic (CHOP vs RUNNER) from experimental patch into first-class config + telemetry + messaging pipeline.
- Ensure operator can see and control behavior via `.env`, `config.py`, messaging, and structured telemetry.

**Changes Made:**
- Added new regime knobs to `.env`:
  - `TS_REGIME_AUTO`, `TS_ADX_UP`, `TS_ADX_DN`, `TS_ATR_UP`, `TS_ATR_DN`
  - `TS_PARTIAL_TP1`, `TS_EXIT_ON_TP1`, `PREPLACE_TP1_PARTIAL`
- Promoted them to `app/config.py` with type-safe defaults and startup summary print.
- Extended `app/messaging.py` to surface `Regime: RUNNER|CHOP` in operator messages.
- Extended `app/telemetry.py` with structured helpers: `log_regime`, `log_tp1_action`, `log_flip_exit`.
- Patched `app/runners/trendscalp_runner.py` to:
  - Classify regime each loop with hysteresis (ADX/ATR thresholds).
  - On TP1 hit: exit full in CHOP, partial+trail in RUNNER.
  - On RUNNER→CHOP flip before TP2: flatten remainder.

**Difference vs Earlier Implementation (Sept 15 patch):**
- **Earlier:** Regime evaluation logic was embedded only inside `trendscalp_runner`, without unified config knobs or structured logging. Messaging had no explicit regime line.
- **Now:** Config is formalized in `.env` + `config.py`; regime surfaced in TG messages; telemetry logs dedicated events; full lifecycle (classify → act → log → message) covered.
- This makes regime handling reproducible, observable, and tunable — not just an internal heuristic.

**Next Steps:**
- Monitor trades tagged with regime lines in logs/Telegram.
- Use telemetry events to evaluate profitability in RUNNER vs CHOP exit styles.
- Adjust thresholds (ADX/ATR) in `.env` as needed based on real performance.

---
## 2025-09-16 15:05 IST — Post‑Entry Validity Guard (PEV) — Design (pre‑TP1)

**Why now:** We observed cases where entry reasons decayed **before TP1**, yet the trade stayed open until SL or TP1. This guard evaluates whether the **reasons we entered** still hold while a trade is young, without fighting the existing **CHOP/RUNNER** logic that governs TP1 actions.

### Scope & Contract
- **Scope:** Applies **only pre‑TP1**. After TP1, the existing regime system remains the sole governor (CHOP → full exit; RUNNER → partial + trail; RUNNER→CHOP flip before TP2 → flatten).
- **No duplication:** PEV does not re‑implement regime; it uses the same features and thresholds where possible.
- **Safety:** Hysteresis + grace to prevent false exits; hard exit only on clear invalidation.

### Entry Snapshot (captured at fill)
Store once in `meta.entry_validity`:
- `side`: LONG|SHORT
- `adx_e`: ADX14(5m) at entry
- `atrpct_e`: ATR14(5m)/Price at entry
- `ema200_side_e`: `above|below` (price vs 200‑EMA 5m & 15m agree)
- `structure_e`: `ok` if HL/HH (long) or LH/LL (short) held at entry
- `ts_e`: entry timestamp

### Signals Used at Runtime (fresh each loop)
- **5m features:** `adx14`, `atr_pct`, price vs 200‑EMA(5m/15m), quick **structure** check (HL/LH over last 2–3 bars).
- **1m micro‑confirm (optional):** last N bars to confirm hard invalidation and avoid wick exits.

### Thresholds (reuse regime bands + small hard bands)
- **Soft degrade:** `adx14 ≤ TS_ADX_DN` **or** `atr_pct ≤ TS_ATR_DN` **or** structure fail.
- **Hard invalidation (exit now):** `adx14 ≤ (TS_ADX_DN − PEV_HARD_ADX_DELTA)` **and** `atr_pct ≤ (TS_ATR_DN × PEV_HARD_ATR_MULT)` **and** EMA200 **side violation** (wrong side) with close‑confirm.
  - Defaults: `PEV_HARD_ADX_DELTA=1.0`, `PEV_HARD_ATR_MULT=0.90`, `PEV_REQUIRE_EMA_SIDE=true`, `PEV_REQUIRE_CLOSE_CONF=true`.

### Decision Ladder (pre‑TP1 only)
1) **HARD invalidation → EXIT** immediately (no grace).
2) Else if **SOFT degrade → WARN** and start/continue **grace**:
   - Wait up to `PEV_GRACE_BARS_5M` bars **and** `PEV_GRACE_MIN_S` seconds.
   - **Recover** if ADX/ATR back above soft bands **and** structure repaired → **OK** (clear warning).
   - If no recovery when grace ends → **EXIT**.
3) Else if **IMPROVED** (≥ entry quality or ≥ RUNNER upgrade band) → **OK** (clear warning; tag improved).
4) Else → **OK** (no action).

### Config (add to .env → config.py)
```
PEV_ENABLED=true
PEV_GRACE_BARS_5M=2
PEV_GRACE_MIN_S=300
PEV_USE_1M_CONFIRM=true
PEV_CONFIRM_1M_BARS=3
PEV_HARD_ADX_DELTA=1.0
PEV_HARD_ATR_MULT=0.90
PEV_REQUIRE_EMA_SIDE=true
PEV_REQUIRE_CLOSE_CONF=true
```

### Wiring & Separation of Concerns
- **Where:** Implement in `app/components/guards.py` as a pure function `post_entry_validity(...)` returning `OK|WARN|EXIT` + diagnostics. Update `meta.pe_guard` (`state`, `warn_since`, `last_reason`).
- **Caller:** `app/runners/trendscalp_runner.py` **before TP1** only:
  - If `PEV_ENABLED` and `hit_tp1 is False`: call guard each loop with fresh features.
  - On `EXIT`: flatten remainder (paper/live), log `PEV_EXIT`, TG: “PEV exit pre‑TP1: …”.
  - On `WARN`: one‑shot `PEV_WARN` (no immediate exit); optional micro tighten can be considered later.
  - On `OK`: nothing.
- **Entry snapshot:** `app/managers/trendscalp_fsm.py` populates `meta.entry_validity` at fill time.

### Interplay with CHOP/RUNNER (from current runbook)
- **PEV never runs after TP1.** Post‑TP1 is governed by existing regime policy:
  - **CHOP:** exit full at TP1.
  - **RUNNER:** partial at TP1, BE+, trail; if later **RUNNER→CHOP** before TP2 and TP1 already hit → flatten.
- **Consistency:** PEV uses the **same ADX/ATR bands** (`TS_ADX_UP/DN`, `TS_ATR_UP/DN`) to judge degrade/improve so decisions align with regime prints.

### Telemetry (one‑shot per state change)
- `manage/PEV_WARN` — payload: `{adx, atr_pct, ema_side, struct, grace_left_bars, grace_left_s}`
- `manage/PEV_EXIT` — payload: `{adx, atr_pct, ema_side, struct, reason: hard|timeout}`
- `manage/PEV_OK` — payload on recovery (clears warning)
- STATUS already includes `regime`, `sl`, `tp1..3`; we won’t spam — debouncer remains in effect.

### Safety & Edge Cases
- Missing features: fall back to ATR% + EMA side; never EXIT on missing data (warn only).
- Side mismatch at entry snapshot: ignore snapshot if side changed (defensive).
- Grace resets if conditions flip to **IMPROVED**.
- DRY_RUN mirrors live: exits actually close paper position (already patched).

### Acceptance (next 10–20 trades)
- Fewer pre‑TP1 SL hits where ADX/ATR/EMA/structure clearly reversed.
- `PEV_WARN` appears sparingly; most resolve to `PEV_OK` or `PEV_EXIT` within the grace window.
- No conflicts with TP1 behavior: CHOP exits at TP1; RUNNER partials at TP1; RUNNER→CHOP flip pre‑TP2 still flattens.

### Rollback
- Set `PEV_ENABLED=false` and restart. No other behavior changes.