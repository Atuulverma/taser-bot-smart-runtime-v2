# app/config.py
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _req(k: str) -> str:
    v = os.getenv(k)
    if v is None or v == "":
        raise RuntimeError(f"Missing env: {k}")
    return v


def _bool(s: str | None) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")


def _floats_csv(name: str, default_csv: str) -> list[float]:
    raw = os.getenv(name, default_csv)
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    out = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(float(tok))
        except Exception:
            raise RuntimeError(f"Invalid float in {name}: {tok!r} from {raw!r}")
    if not out:
        raise RuntimeError(f"{name} parsed empty from {raw!r}")
    return out


# ===== Core =====
PAIR = _req("PAIR")
EXCHANGE_ID = _req("EXCHANGE_ID")
DRY_RUN = _bool(_req("DRY_RUN"))
MAX_LEVERAGE = int(_req("MAX_LEVERAGE"))
TASER_DB = _req("TASER_DB")
DB_PATH = TASER_DB

# ===== Sizing / Risk =====
SIZING_MODE = _req("SIZING_MODE")
CAPITAL_FRACTION = float(_req("CAPITAL_FRACTION"))
RISK_PCT = float(_req("RISK_PCT"))
RSI_OB = int(_req("RSI_OB"))
RSI_OS = int(_req("RSI_OS"))
MIN_QTY = float(os.getenv("MIN_QTY", "1"))
MIN_SL_ABS = float(os.getenv("MIN_SL_ABS", "0.00"))

# Live sizing toggle (when live, prefer free margin sizing)
LIVE_SIZING_USE_FREE_MARGIN = _bool(os.getenv("LIVE_SIZING_USE_FREE_MARGIN", "true"))

# ===== Entry thresholds =====
NEAR_PDH_PCT = float(_req("NEAR_PDH_PCT"))
NEAR_AVWAP_PCT = float(_req("NEAR_AVWAP_PCT"))
NEAR_VWAP_PCT_MIN = float(_req("NEAR_VWAP_PCT_MIN"))
NEAR_VWAP_PCT_MAX = float(_req("NEAR_VWAP_PCT_MAX"))
ATR_NEAR_MULT = float(_req("ATR_NEAR_MULT"))
VWAP_RECLAIM_ATR_MULT = float(_req("VWAP_RECLAIM_ATR_MULT"))
AVWAP_RECLAIM_ATR_MULT = float(_req("AVWAP_RECLAIM_ATR_MULT"))
AGGRESSION = os.getenv("AGGRESSION", "balanced").strip().lower()
PERSIST_BARS = int(os.getenv("PERSIST_BARS", "2"))
MIN_R_MULT = float(os.getenv("MIN_R_MULT", "1.4"))

# ===== Scheduler / pacing =====
SCAN_INTERVAL_SECONDS = int(_req("SCAN_INTERVAL_SECONDS"))

# ===== Exchange & Telegram =====
# ===== OHLCV / Market Data =====
OHLCV_TIMEFRAME = os.getenv("OHLCV_TIMEFRAME", "5m").strip()
OHLCV_FETCH_LIMIT = int(os.getenv("OHLCV_FETCH_LIMIT", "600"))  # bars to fetch per pull
# ensure at least this many bars exist for indicators
OHLCV_BACKFILL_MIN = int(os.getenv("OHLCV_BACKFILL_MIN", "300"))

DELTA_REGION = os.getenv("DELTA_REGION", "india")
DELTA_API_KEY = os.getenv("DELTA_API_KEY", "").strip()
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "").strip()
DELTA_WS_URL = os.getenv("DELTA_WS_URL", "").strip()
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ===== Single-position / hygiene =====
SINGLE_POSITION_MODE = _bool(os.getenv("SINGLE_POSITION_MODE", "true"))
MANAGE_POLL_SECONDS = int(os.getenv("MANAGE_POLL_SECONDS", "3"))
FAST_SCAN_AFTER_TP1 = int(os.getenv("FAST_SCAN_AFTER_TP1", "2"))

STATUS_ON_CHANGE_ONLY = _bool(os.getenv("STATUS_ON_CHANGE_ONLY", "true"))
STATUS_INTERVAL_SECONDS = int(os.getenv("STATUS_INTERVAL_SECONDS", "60"))

PLACE_TP3_LIMIT = _bool(os.getenv("PLACE_TP3_LIMIT", "true"))
DYNAMIC_TP_EXTEND = _bool(os.getenv("DYNAMIC_TP_EXTEND", "true"))

# ===== Heatmap =====
HEATMAP_RETENTION_DAYS = int(os.getenv("HEATMAP_RETENTION_DAYS", "90"))
HEATMAP_SL_PAD_MULT = float(os.getenv("HEATMAP_SL_PAD_MULT", "0.95"))
HEATMAP_TRAIL_GRACE_SEC = int(os.getenv("HEATMAP_TRAIL_GRACE_SEC", "150"))

# ==================================================================
#                  SL / TP — SINGLE SOURCE OF TRUTH
# ==================================================================
# Volatility-aware SL blending
SL_MIX_ALPHA = float(os.getenv("SL_MIX_ALPHA", "0.55"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.10"))
SL_NOISE_MULT = float(os.getenv("SL_NOISE_MULT", "2.50"))
SL_NOISE_BARS_1M = int(os.getenv("SL_NOISE_BARS_1M", "10"))

# Absolute stop rails (as % of price)
MIN_SL_PCT = float(os.getenv("MIN_SL_PCT", "0.0060"))
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "0.0130"))

# Fees / cushion
# DEPRECATED: not used by current managers; kept for backward-compat.
# Prefer FEES_PCT_PAD for BE floor.
FEE_PCT = float(os.getenv("FEE_PCT", "0.0005"))
FEE_PAD_MULT = float(os.getenv("FEE_PAD_MULT", "2.0"))

# TP mode & levels
TP_MODE = os.getenv("TP_MODE", "atr").strip().lower()  # 'atr' | 'r'
TP1_ATR_MULT = float(os.getenv("TP1_ATR_MULT", "0.60"))
TP2_ATR_MULT = float(os.getenv("TP2_ATR_MULT", "1.00"))
TP3_ATR_MULT = float(os.getenv("TP3_ATR_MULT", "1.50"))
TP_R_MULTIS = _floats_csv("TP_R_MULTIS", "1.0,1.8,2.6")

# Mode‑adaptive TP sizing (auto chop vs rally)
MODE_ADAPT_ENABLED = _bool(os.getenv("MODE_ADAPT_ENABLED", "true"))
MODE_CHOP_ATR_PCT_MAX = float(os.getenv("MODE_CHOP_ATR_PCT_MAX", "0.0025"))  # 0.25% of price
MODE_CHOP_ADX_MAX = float(os.getenv("MODE_CHOP_ADX_MAX", "25"))
MODE_CHOP_TP_ATR_MULTS = _floats_csv("MODE_CHOP_TP_ATR_MULTS", "0.60,1.00,1.50")
MODE_RALLY_TP_ATR_MULTS = _floats_csv("MODE_RALLY_TP_ATR_MULTS", "0.90,1.60,2.60")

# TP locking / trail behavior (new style) — default is 'trail_fracR'.
# Legacy $0.25/$0.50 absolute locks are removed; BE floor via FEES_PCT_PAD.
TP_LOCK_STYLE = os.getenv("TP_LOCK_STYLE", "trail_fracR").strip()
TP_LOCK_CONFIRM_BARS = int(os.getenv("TP_LOCK_CONFIRM_BARS", "2"))
TP_CONFIRM_USE_CLOSED_ONLY = _bool(os.getenv("TP_CONFIRM_USE_CLOSED_ONLY", "true"))
# DEPRECATED: BE buffer now handled by FEES_PCT_PAD in surveillance; keep only for legacy configs.
TP1_LOCK_FRACR = float(os.getenv("TP1_LOCK_FRACR", "0.35"))
TP2_LOCK_FRACR = float(os.getenv("TP2_LOCK_FRACR", "0.75"))
TP1_LOCK_ATR_MULT = float(os.getenv("TP1_LOCK_ATR_MULT", "0.45"))
TP2_LOCK_ATR_MULT = float(os.getenv("TP2_LOCK_ATR_MULT", "0.35"))
TP_LOCK_GRACE_SEC = int(os.getenv("TP_LOCK_GRACE_SEC", "3"))
BE_BUFFER_PCT = float(os.getenv("BE_BUFFER_PCT", "0.0015"))

# ===== Post‑TP1 grace & trailing style =====
# bars to wait after TP1 before tightening SL
POST_TP1_SL_DELAY_BARS = int(os.getenv("POST_TP1_SL_DELAY_BARS", "3"))
# BE +/- eps cushion in ATR units at TP1
BE_EPS_ATR_MULT = float(os.getenv("BE_EPS_ATR_MULT", "0.10"))
TRAIL_STYLE = os.getenv("TRAIL_STYLE", "structure").strip()  # 'structure' | 'fracR'

# Structure (Chandelier-like) trailing params
CHAND_N_PRE_TP2 = int(os.getenv("CHAND_N_PRE_TP2", "9"))
CHAND_K_PRE_TP2 = float(os.getenv("CHAND_K_PRE_TP2", "1.2"))
CHAND_N_POST_TP2 = int(os.getenv("CHAND_N_POST_TP2", "7"))
CHAND_K_POST_TP2 = float(os.getenv("CHAND_K_POST_TP2", "0.8"))
CHAND_N_POST_TP3 = int(os.getenv("CHAND_N_POST_TP3", "5"))
CHAND_K_POST_TP3 = float(os.getenv("CHAND_K_POST_TP3", "0.6"))

# Momentum stall exit near target
STALL_BARS = int(os.getenv("STALL_BARS", "3"))
STALL_NEAR_TP_ATR = float(os.getenv("STALL_NEAR_TP_ATR", "0.50"))
STALL_RSI_CONFIRM = _bool(os.getenv("STALL_RSI_CONFIRM", "true"))
STALL_TP_EPS = float(os.getenv("STALL_TP_EPS", "0.02"))

# ---- SL/TP safety guards & rate limits (matches .env) ----
# (Scalper reads SCALP_ABS_LOCK_USD; TrendScalp may pause via TRENDSCALP_PAUSE_ABS_LOCKS)
# min SL gap from price in ATR units
SL_MIN_GAP_ATR_MULT = float(os.getenv("SL_MIN_GAP_ATR_MULT", "0.35"))
# fallback min gap (~0.12%)
SL_MIN_GAP_PCT = float(os.getenv("SL_MIN_GAP_PCT", "0.0012"))
# after TP1, never trail below BE after fees
LOCK_NEVER_WORSE_THAN_BE = _bool(os.getenv("LOCK_NEVER_WORSE_THAN_BE", "true"))
# round‑trip fees cushion for BE floor/ceiling
FEES_PCT_PAD = float(os.getenv("FEES_PCT_PAD", "0.0010"))
# Messaging/ops de-dupe tolerances & cooldowns (used by surveillance TP/SL gates)
# SL change is ignored if within this absolute delta
SL_EPS = float(os.getenv("SL_EPS", "0.0003"))
SL_MIN_INTERVAL_S = float(os.getenv("SL_MIN_INTERVAL_S", "20"))
# TP tuple change ignored if within this absolute delta
TP_EPS = float(os.getenv("TP_EPS", "0.0003"))
TP_MIN_INTERVAL_S = float(os.getenv("TP_MIN_INTERVAL_S", "30"))
# ignore SL updates smaller than this ($)
TS_MIN_SL_CHANGE_ABS = float(os.getenv("TS_MIN_SL_CHANGE_ABS", "0.02"))
# NOTE: Not used by TrendScalp; only the optional scalper reads these.
# min hard BE+ lock once in profit
SCALP_ABS_LOCK_USD = float(os.getenv("SCALP_ABS_LOCK_USD", "0.50"))

# TrendScalp SL step/buffer knobs
# minimum ATR step required to move SL
TS_SL_MIN_STEP_ATR = float(os.getenv("TS_SL_MIN_STEP_ATR", "0.12"))
# buffer from structure/heatmap when tightening
TS_SL_MIN_BUFFER_ATR = float(os.getenv("TS_SL_MIN_BUFFER_ATR", "0.45"))

# rate-limit SL updates
SL_TIGHTEN_COOLDOWN_SEC = int(os.getenv("SL_TIGHTEN_COOLDOWN_SEC", "55"))
# rate-limit TP extensions
TP_EXTEND_COOLDOWN_SEC = int(os.getenv("TP_EXTEND_COOLDOWN_SEC", "55"))
# min ATR improvement to extend TP2/3
TP_EXTEND_MIN_DELTA_ATR = float(os.getenv("TP_EXTEND_MIN_DELTA_ATR", "0.20"))

# Legacy lock modes (used only if TP_LOCK_STYLE='to_tp1' or legacy helpers are called)
# NOTE: With default TP_LOCK_STYLE='trail_fracR', these are ignored.
# Safe to leave for backward-compat.
TP1_LOCK_MODE = os.getenv("TP1_LOCK_MODE", "breakeven_buffer").strip()
TP2_LOCK_MODE = os.getenv("TP2_LOCK_MODE", "tp2_buffer").strip()
TP1_LOCK_OFFSET = float(os.getenv("TP1_LOCK_OFFSET", "0.10"))
TP2_LOCK_OFFSET = float(os.getenv("TP2_LOCK_OFFSET", "0.12"))

# ===== Invalidation hysteresis =====
INVALIDATION_MODE = os.getenv("INVALIDATION_MODE", "hysteresis").strip()
INVALIDATE_MIN_HOLD_SEC = int(os.getenv("INVALIDATE_MIN_HOLD_SEC", "45"))
INVALIDATE_REQUIRE_BARS = int(os.getenv("INVALIDATE_REQUIRE_BARS", "2"))
INVALIDATE_PRICE_DRIFT_PCT = float(os.getenv("INVALIDATE_PRICE_DRIFT_PCT", "0.0015"))
INVALIDATE_REQUIRE_OPPOSITE = _bool(os.getenv("INVALIDATE_REQUIRE_OPPOSITE", "true"))
INVALIDATE_USE_TREND = _bool(os.getenv("INVALIDATE_USE_TREND", "true"))

# ===== Flip / Profit-decay exit =====
FLIP_ENABLED = _bool(os.getenv("FLIP_ENABLED", "false"))
FLIP_MIN_HOLD_SEC = int(os.getenv("FLIP_MIN_HOLD_SEC", "240"))
FLIP_MIN_PEAK_R = float(os.getenv("FLIP_MIN_PEAK_R", "0.60"))
FLIP_DECAY_FRAC = float(os.getenv("FLIP_DECAY_FRAC", "0.50"))
FLIP_REQUIRE_OPPOSITE = _bool(os.getenv("FLIP_REQUIRE_OPPOSITE", "true"))
FLIP_REQUIRE_MIN_R = float(os.getenv("FLIP_REQUIRE_MIN_R", "1.2"))
FLIP_COOLDOWN_SEC = int(os.getenv("FLIP_COOLDOWN_SEC", "240"))
FLIP_MAX_PER_TRADE = int(os.getenv("FLIP_MAX_PER_TRADE", "1"))

# ===== Dynamic Avoid =====

DYN_AVOID_ENABLED = _bool(os.getenv("DYN_AVOID_ENABLED", "true"))
AVOID_LOOKBACK_BARS = int(os.getenv("AVOID_LOOKBACK_BARS", "120"))
CHOP_MIN_FLIPS = int(os.getenv("CHOP_MIN_FLIPS", "12"))
CHOP_MAX_WIDTH_PCT = float(os.getenv("CHOP_MAX_WIDTH_PCT", "0.006"))
CONF_MAX_SPREAD_PCT = float(os.getenv("CONF_MAX_SPREAD_PCT", "0.004"))

# ===== Re-entry (explicit 5m cooldown used by TrendScalp) =====
# Keep legacy TS_COOLDOWN_BARS for backward-compat but prefer REENTRY_COOLDOWN_BARS_5M
REENTRY_COOLDOWN_BARS_5M = int(
    os.getenv("REENTRY_COOLDOWN_BARS_5M", os.getenv("TS_COOLDOWN_BARS", "1"))
)

# ===== Effective toggles (do not silently override .env; just compute & log) =====
# FLIP/Dynamic Avoid can conflict with TrendScalp's own manager & filters.
FLIP_EFFECTIVE = FLIP_ENABLED  # set .env FLIP_ENABLED=false to avoid double exit logic
# set .env DYN_AVOID_ENABLED=false to rely on TrendScalp gates only
DYN_AVOID_EFFECTIVE = DYN_AVOID_ENABLED

# ===== Execution hygiene (often used by scheduler/execution) =====
MIN_REENTRY_SECONDS = int(os.getenv("MIN_REENTRY_SECONDS", "90"))
BLOCK_REENTRY_PCT = float(os.getenv("BLOCK_REENTRY_PCT", "0.004"))
REQUIRE_NEW_BAR = _bool(os.getenv("REQUIRE_NEW_BAR", "true"))

# ===== (Optional) Scalper toggles (safe no-ops if unused) =====
SCALP_ENABLED = _bool(os.getenv("SCALP_ENABLED", "false"))
SCALP_MIN_TICK_PROFIT = float(os.getenv("SCALP_MIN_TICK_PROFIT", "0.50"))
SCALP_MAX_TICK_PROFIT = float(os.getenv("SCALP_MAX_TICK_PROFIT", "0.75"))
SCALP_TIMEFRAME = os.getenv("SCALP_TIMEFRAME", "1m")
SCALP_MAX_HOLD_SECONDS = int(os.getenv("SCALP_MAX_HOLD_SECONDS", "420"))

# ===== Telemetry / Audit (optional but loaded if present) =====
OPENAI_USE = _bool(os.getenv("OPENAI_USE", "false"))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
AUDIT_DEBOUNCE_SECONDS = int(os.getenv("AUDIT_DEBOUNCE_SECONDS", "20"))
AUDIT_CACHE_TTL_SECONDS = int(os.getenv("AUDIT_CACHE_TTL_SECONDS", "600"))
AUDIT_SAMPLE_TAIL_N = int(os.getenv("AUDIT_SAMPLE_TAIL_N", "24"))
AUDIT_MAX_LEVELS = int(os.getenv("AUDIT_MAX_LEVELS", "10"))

# Structured telemetry switches
TELEMETRY_JSON_IN_STATUS = _bool(os.getenv("TELEMETRY_JSON_IN_STATUS", "true"))
TELEMETRY_MFE_MAE = _bool(os.getenv("TELEMETRY_MFE_MAE", "true"))
ANALYTICS_TRACK_PEAKS = _bool(os.getenv("ANALYTICS_TRACK_PEAKS", "true"))

FLOW_ENABLED = _bool(os.getenv("FLOW_ENABLED", "true"))
FLOW_BE_AT_R_PCT = float(os.getenv("FLOW_BE_AT_R_PCT", "0.90"))
FLOW_TP2_R_MULT = float(os.getenv("FLOW_TP2_R_MULT", "1.6"))
FLOW_TP3_R_MULT = float(os.getenv("FLOW_TP3_R_MULT", "2.6"))
FLOW_REPLACE_TPS = _bool(os.getenv("FLOW_REPLACE_TPS", "true"))

PAPER_USE_START_BALANCE = _bool(os.getenv("PAPER_USE_START_BALANCE", "true"))
PAPER_START_BALANCE = float(os.getenv("PAPER_START_BALANCE", "1000"))
PAPER_FEE_PCT = float(os.getenv("PAPER_FEE_PCT", os.getenv("FEE_PCT", "0.0005")))
# Warn in DRY_RUN if PAPER_USE_START_BALANCE is not explicitly set in the env
# Warn in DRY_RUN if PAPER_USE_START_BALANCE is not explicitly set in the env
if DRY_RUN and os.getenv("PAPER_USE_START_BALANCE") is None:
    print(
        "[CONFIG] DRY_RUN=true but PAPER_USE_START_BALANCE not set; defaulting to true. "
        "Set PAPER_USE_START_BALANCE=true in .env for clarity.",
        file=sys.stderr,
    )
# Enforce live sizing rule: when DRY_RUN is false, ignore paper start balance and use free margin
if not DRY_RUN:
    PAPER_USE_START_BALANCE = False
    LIVE_SIZING_USE_FREE_MARGIN = True

# [OPPORTUNISTIC_TWEAK] global toggle (disable to revert to strict behavior quickly)
OPPORTUNISTIC_TWEAKS = _bool(os.getenv("OPPORTUNISTIC_TWEAKS", "true"))

# Engine order (scheduler may use this; default is trendscalp only)
ENGINE_ORDER = [s.strip() for s in os.getenv("ENGINE_ORDER", "trendscalp").split(",") if s.strip()]

# ===== Dashboard / Engine-split export =====
DASH_ENGINE_SPLIT_ENABLED = _bool(os.getenv("DASH_ENGINE_SPLIT_ENABLED", "true"))
DASH_ENGINE_SPLIT_LOOKBACK = os.getenv("DASH_ENGINE_SPLIT_LOOKBACK", "24h").strip()  # '24h' or '7d'
DASH_ENGINE_SPLIT_CSV = os.getenv("DASH_ENGINE_SPLIT_CSV", "runtime/engine_summary_24h.csv").strip()

# TrendSignal
# ===== TrendScalp switches =====
TRENDSCALP_ENABLED = _bool(os.getenv("TRENDSCALP_ENABLED", "false"))
TRENDSCALP_ONLY = _bool(os.getenv("TRENDSCALP_ONLY", "false"))
TRENDSCALP_USE_AVOID_ZONES = _bool(os.getenv("TRENDSCALP_USE_AVOID_ZONES", "false"))
TRENDSCALP_SESSION_HOURS = int(os.getenv("TRENDSCALP_SESSION_HOURS", "24"))

# Lorentzian
TS_NEIGHBORS = int(os.getenv("TS_NEIGHBORS", "8"))
TS_MAX_BACK = int(os.getenv("TS_MAX_BACK", "2000"))
TS_FEATURE_COUNT = int(os.getenv("TS_FEATURE_COUNT", "5"))
TS_USE_VOL_FILTER = _bool(os.getenv("TS_USE_VOL_FILTER", "true"))
TS_USE_REGIME_FILTER = _bool(os.getenv("TS_USE_REGIME_FILTER", "true"))
TS_USE_ADX_FILTER = _bool(os.getenv("TS_USE_ADX_FILTER", "false"))
TS_USE_RSI_FILTER = _bool(os.getenv("TS_USE_RSI_FILTER", "true"))

# EMA/SMA filters (optional)
TS_EMA_FILTER = _bool(os.getenv("TS_EMA_FILTER", "false"))
TS_EMA_PERIOD = int(os.getenv("TS_EMA_PERIOD", "200"))
TS_EMA_SLOW = int(os.getenv("TS_EMA_SLOW", "20"))
TS_SMA_FILTER = _bool(os.getenv("TS_SMA_FILTER", "false"))
TS_SMA_PERIOD = int(os.getenv("TS_SMA_PERIOD", "200"))

# Kernel (placeholder; no-op in TrendScalp unless explicitly used elsewhere)
TS_USE_KERNEL = _bool(os.getenv("TS_USE_KERNEL", "true"))
TS_KERNEL_SMOOTH = _bool(os.getenv("TS_KERNEL_SMOOTH", "false"))
TS_K_H = int(os.getenv("TS_K_H", "8"))
TS_K_R = float(os.getenv("TS_K_R", "8.0"))
TS_K_X = int(os.getenv("TS_K_X", "25"))
TS_K_LAG = int(os.getenv("TS_K_LAG", "2"))

# Trendlines clone
TS_TL_LOOKBACK = int(os.getenv("TS_TL_LOOKBACK", "12"))
TS_TL_SLOPE_METHOD = os.getenv("TS_TL_SLOPE_METHOD", "atr")
TS_TL_SLOPE_MULT = float(os.getenv("TS_TL_SLOPE_MULT", "1.4"))
TS_TL_SHOW_EXT = _bool(os.getenv("TS_TL_SHOW_EXT", "true"))

# Entry/Exit
TS_REQUIRE_BOTH = _bool(os.getenv("TS_REQUIRE_BOTH", "true"))
TS_PULLBACK_PCT = float(os.getenv("TS_PULLBACK_PCT", "0.0035"))
TS_WAI_MIN = float(os.getenv("TS_WAI_MIN", "0.50"))
TS_COOLDOWN_BARS = int(os.getenv("TS_COOLDOWN_BARS", "4"))

TS_TP_R = _floats_csv("TS_TP_R", "0.9,1.6,2.6")
TS_STOP_MODE = os.getenv("TS_STOP_MODE", "trendline")

# TrendScalp Milestone Trailing (new)
TS_MILESTONE_MODE = _bool(os.getenv("TS_MILESTONE_MODE", "true"))
TS_MS_STEP_R = float(os.getenv("TS_MS_STEP_R", "0.5"))
TS_MS_LOCK_DELTA_R = float(os.getenv("TS_MS_LOCK_DELTA_R", "0.25"))
TS_TP2_LOCK_FRACR = float(os.getenv("TS_TP2_LOCK_FRACR", "0.70"))
TS_POST_TP2_ATR_MULT = float(os.getenv("TS_POST_TP2_ATR_MULT", "0.50"))

# TrendScalp regime controls (Chop vs Runner)
TS_REGIME_AUTO = _bool(os.getenv("TS_REGIME_AUTO", "true"))
TS_ADX_UP = float(os.getenv("TS_ADX_UP", "26.0"))
TS_ADX_DN = float(os.getenv("TS_ADX_DN", "23.0"))
TS_ATR_UP = float(os.getenv("TS_ATR_UP", "0.0040"))  # 0.40% of price
TS_ATR_DN = float(os.getenv("TS_ATR_DN", "0.0035"))  # 0.35% of price
TS_PARTIAL_TP1 = float(os.getenv("TS_PARTIAL_TP1", "0.50"))
TS_EXIT_ON_TP1 = _bool(os.getenv("TS_EXIT_ON_TP1", "false"))
PREPLACE_TP1_PARTIAL = _bool(os.getenv("PREPLACE_TP1_PARTIAL", "false"))

# Post‑Entry Validity Guard (pre‑TP1 only)
PEV_ENABLED = _bool(os.getenv("PEV_ENABLED", "true"))
PEV_GRACE_BARS_5M = int(os.getenv("PEV_GRACE_BARS_5M", "2"))
PEV_GRACE_MIN_S = int(os.getenv("PEV_GRACE_MIN_S", "300"))
PEV_USE_1M_CONFIRM = _bool(os.getenv("PEV_USE_1M_CONFIRM", "true"))
PEV_CONFIRM_1M_BARS = int(os.getenv("PEV_CONFIRM_1M_BARS", "3"))
PEV_HARD_ADX_DELTA = float(os.getenv("PEV_HARD_ADX_DELTA", "1.0"))
PEV_HARD_ATR_MULT = float(os.getenv("PEV_HARD_ATR_MULT", "0.90"))
PEV_REQUIRE_EMA_SIDE = _bool(os.getenv("PEV_REQUIRE_EMA_SIDE", "true"))
PEV_REQUIRE_CLOSE_CONF = _bool(os.getenv("PEV_REQUIRE_CLOSE_CONF", "true"))
# New Post-Entry Validity Guard knobs
PEV_EXIT_IMMEDIATE = _bool(os.getenv("PEV_EXIT_IMMEDIATE", "false"))
PEV_WAIT_BARS = int(os.getenv("PEV_WAIT_BARS", "1"))
PEV_USE_RECENT = _bool(os.getenv("PEV_USE_RECENT", "true"))

# TrendScalp runtime confirmations / venue checks (new)
# 0 = touch, >0 = require N 1m closes beyond TP
TP_HIT_CONFIRM_BARS = int(os.getenv("TP_HIT_CONFIRM_BARS", "0"))
TS_CHECK_POS_EVERY_S = int(os.getenv("TS_CHECK_POS_EVERY_S", "10"))  # 0 disables venue flat check

# TrendScalp exit confirmation / hysteresis
TS_EXIT_USE_CLOSE = _bool(os.getenv("TS_EXIT_USE_CLOSE", "true"))
TS_EXIT_CONFIRM_BARS = int(os.getenv("TS_EXIT_CONFIRM_BARS", "2"))
TS_REVERSAL_ATR_PAD = float(os.getenv("TS_REVERSAL_ATR_PAD", "0.2"))

# ---- Pine-parity filters & exits (TrendScalp) ----
# ATR14(5m)/price >= 0.20%
TS_VOL_FLOOR_PCT = float(os.getenv("TS_VOL_FLOOR_PCT", "0.0020"))
TS_ADX_MIN = float(os.getenv("TS_ADX_MIN", "15"))
TS_ADX_SOFT = float(os.getenv("TS_ADX_SOFT", "12"))
TS_OVERRIDE_EMA_RSI = _bool(os.getenv("TS_OVERRIDE_EMA_RSI", "false"))
TS_TL_WIDTH_ATR_MULT = float(os.getenv("TS_TL_WIDTH_ATR_MULT", "0.45"))
# Adaptive regime (relax TL width threshold under strong-trend ADX)
TS_ADAPT_REGIME = _bool(os.getenv("TS_ADAPT_REGIME", "true"))
TS_ADAPT_ADX1 = float(os.getenv("TS_ADAPT_ADX1", "30"))
TS_ADAPT_ADX2 = float(os.getenv("TS_ADAPT_ADX2", "40"))
TS_ADAPT_MULT1 = float(os.getenv("TS_ADAPT_MULT1", "0.35"))
TS_ADAPT_MULT2 = float(os.getenv("TS_ADAPT_MULT2", "0.25"))
TS_RSI15_NEUTRAL_LO = float(os.getenv("TS_RSI15_NEUTRAL_LO", "45"))
TS_RSI15_NEUTRAL_HI = float(os.getenv("TS_RSI15_NEUTRAL_HI", "55"))
TS_RSI_OVERHEAT_HI = float(os.getenv("TS_RSI_OVERHEAT_HI", "72"))
TS_RSI_OVERHEAT_LO = float(os.getenv("TS_RSI_OVERHEAT_LO", "35"))
# MA alignment knobs (new)
# if true, also require 15m EMA alignment
TS_MA_REQUIRE_15M = _bool(os.getenv("TS_MA_REQUIRE_15M", "false"))
# extra buffer vs EMA to accept entry (~0.15%)
TS_MA_BUFFER_PCT = float(os.getenv("TS_MA_BUFFER_PCT", "0.0015"))
TRENDSCALP_PAUSE_ABS_LOCKS = _bool(os.getenv("TRENDSCALP_PAUSE_ABS_LOCKS", "true"))
TS_BE_ARM_R = float(os.getenv("TS_BE_ARM_R", "0.5"))
TS_GIVEBACK_ARM_R = float(os.getenv("TS_GIVEBACK_ARM_R", "1.2"))
TS_GIVEBACK_FRAC = float(os.getenv("TS_GIVEBACK_FRAC", "0.40"))
TS_REVERSAL_MIN_R = float(os.getenv("TS_REVERSAL_MIN_R", "0.50"))
TS_REVERSAL_ADX_MIN = float(os.getenv("TS_REVERSAL_ADX_MIN", "22"))


# ===== Enhanced Exit Controls =====
GLOBAL_NO_TRAIL_BEFORE_TP1 = _bool(os.getenv("GLOBAL_NO_TRAIL_BEFORE_TP1", "false"))
RATCHET_MIN_R = float(os.getenv("RATCHET_MIN_R", "0.60"))
RATCHET_GRACE_SEC = int(os.getenv("RATCHET_GRACE_SEC", "150"))
TP_FREEZE_SL_TIGHT_R = float(os.getenv("TP_FREEZE_SL_TIGHT_R", "0.7"))
SCALP_TMAX_MIN = int(os.getenv("SCALP_TMAX_MIN", "0"))  # minutes, 0=disabled
SCALP_TMAX_MFE_R = float(os.getenv("SCALP_TMAX_MFE_R", "0.5"))
MIN_SL_FRAC = float(os.getenv("MIN_SL_FRAC", "0.005"))

# Debug
TG_DEBUG_VALIDATORS = _bool(os.getenv("TG_DEBUG_VALIDATORS", "false"))


# ---------------- Sanity checks / summary ----------------
try:
    assert 0.0 < MIN_SL_PCT < MAX_SL_PCT < 0.2, "SL rails look wrong"
    assert 0.0 <= SL_MIX_ALPHA <= 1.0, "SL_MIX_ALPHA must be 0..1"
except AssertionError as e:
    print(f"[CONFIG] Sanity check failed: {e}", file=sys.stderr)

# Print config summary with lines wrapped to <=100 chars and no ambiguous names or semicolons
print("[CONFIG] SL/TP => ", file=sys.stderr)
print(
    f"SL_MIX_ALPHA={SL_MIX_ALPHA} | SL_ATR_MULT={SL_ATR_MULT} | SL_NOISE_MULT={SL_NOISE_MULT}",
    file=sys.stderr,
)
print(
    f"MIN_SL_PCT={MIN_SL_PCT} | MAX_SL_PCT={MAX_SL_PCT} | TP_MODE={TP_MODE} | "
    f"TP_R_MULTIS={TP_R_MULTIS}",
    file=sys.stderr,
)
print("[CONFIG] TP_MODE => ", file=sys.stderr)
print(
    f"MODE={TP_MODE} | "
    f"ATR_MULTS={[TP1_ATR_MULT, TP2_ATR_MULT, TP3_ATR_MULT]} | "
    f"ADAPT={MODE_ADAPT_ENABLED}",
    file=sys.stderr,
)
print(
    "CHOP_THRESH="
    f"{{'ATR%': {MODE_CHOP_ATR_PCT_MAX}, "
    f"'ADX': {MODE_CHOP_ADX_MAX}}} | "
    f"CHOP_MULTS={MODE_CHOP_TP_ATR_MULTS} | "
    f"RALLY_MULTS={MODE_RALLY_TP_ATR_MULTS}",
    file=sys.stderr,
)
print("[CONFIG] DASH => ", file=sys.stderr)
print(
    f"DASH_ENGINE_SPLIT_ENABLED={DASH_ENGINE_SPLIT_ENABLED} | "
    f"LOOKBACK={DASH_ENGINE_SPLIT_LOOKBACK} | "
    f"CSV={DASH_ENGINE_SPLIT_CSV} | "
    f"ENGINE_ORDER={ENGINE_ORDER}",
    file=sys.stderr,
)
print("[CONFIG] GUARDS => ", file=sys.stderr)
print(
    f"TS_MIN_SL_CHANGE_ABS={TS_MIN_SL_CHANGE_ABS} | "
    f"SCALP_ABS_LOCK_USD={SCALP_ABS_LOCK_USD} | "
    f"SL_EPS={SL_EPS} | TP_EPS={TP_EPS}",
    file=sys.stderr,
)
print(
    f"TS_SL_MIN_STEP_ATR={TS_SL_MIN_STEP_ATR} | TS_SL_MIN_BUFFER_ATR={TS_SL_MIN_BUFFER_ATR}",
    file=sys.stderr,
)
print("[CONFIG] TS_MA => ", file=sys.stderr)
print(
    f"REQUIRE_15M={TS_MA_REQUIRE_15M} | BUFFER_PCT={TS_MA_BUFFER_PCT}",
    file=sys.stderr,
)
print("[CONFIG] TS_ADAPT => ", file=sys.stderr)
print(
    f"REGIME={TS_ADAPT_REGIME} | ADX1={TS_ADAPT_ADX1} | ADX2={TS_ADAPT_ADX2}",
    file=sys.stderr,
)
print(
    f"MULT1={TS_ADAPT_MULT1} | MULT2={TS_ADAPT_MULT2}",
    file=sys.stderr,
)
print("[CONFIG] TS_FILTERS => ", file=sys.stderr)
print(
    f"VOL={TS_USE_VOL_FILTER} | "
    f"REGIME={TS_USE_REGIME_FILTER} | "
    f"ADX={TS_USE_ADX_FILTER} | "
    f"RSI={TS_USE_RSI_FILTER}",
    file=sys.stderr,
)
print("[CONFIG] TS_MILESTONE => ", file=sys.stderr)
print(
    f"MODE={TS_MILESTONE_MODE} | STEP_R={TS_MS_STEP_R} | LOCK_DELTA_R={TS_MS_LOCK_DELTA_R}",
    file=sys.stderr,
)
print(
    f"TP2_FRACR={TS_TP2_LOCK_FRACR} | POST_TP2_ATR_MULT={TS_POST_TP2_ATR_MULT}",
    file=sys.stderr,
)

# Regime controls summary
print("[CONFIG] TS_REGIME => ", file=sys.stderr)
print(
    f"AUTO={TS_REGIME_AUTO} | ADX_UP={TS_ADX_UP} ADX_DN={TS_ADX_DN} | "
    f"ATR_UP={TS_ATR_UP} ATR_DN={TS_ATR_DN} | PARTIAL_TP1={TS_PARTIAL_TP1} | "
    f"EXIT_ON_TP1={TS_EXIT_ON_TP1} | PREPLACE_PARTIAL={PREPLACE_TP1_PARTIAL}",
    file=sys.stderr,
)
print("[CONFIG] TS_PEV => ", file=sys.stderr)
print(
    f"ENABLED={PEV_ENABLED} | GRACE_BARS_5M={PEV_GRACE_BARS_5M} GRACE_MIN_S={PEV_GRACE_MIN_S} | "
    f"USE_1M_CONFIRM={PEV_USE_1M_CONFIRM} CONF_1M_BARS={PEV_CONFIRM_1M_BARS} | "
    f"HARD_ADX_DELTA={PEV_HARD_ADX_DELTA} HARD_ATR_MULT={PEV_HARD_ATR_MULT} | "
    f"REQ_EMA_SIDE={PEV_REQUIRE_EMA_SIDE} REQ_CLOSE_CONF={PEV_REQUIRE_CLOSE_CONF} | "
    f"EXIT_IMMEDIATE={PEV_EXIT_IMMEDIATE} WAIT_BARS={PEV_WAIT_BARS} USE_RECENT={PEV_USE_RECENT}",
    file=sys.stderr,
)
print("[CONFIG] TS_RUNTIME => ", file=sys.stderr)
print(
    f"TP_HIT_CONFIRM_BARS={TP_HIT_CONFIRM_BARS} | TS_CHECK_POS_EVERY_S={TS_CHECK_POS_EVERY_S}",
    file=sys.stderr,
)
print("[CONFIG] STATUS_EMIT => ", file=sys.stderr)
print(
    f"ON_CHANGE_ONLY={STATUS_ON_CHANGE_ONLY} | INTERVAL_S={STATUS_INTERVAL_SECONDS}",
    file=sys.stderr,
)
if not DRY_RUN:
    print("[CONFIG] Live mode: forcing PAPER_USE_START_BALANCE=false", file=sys.stderr)
    print("[CONFIG] Live mode: forcing LIVE_SIZING_USE_FREE_MARGIN=true", file=sys.stderr)
print("[CONFIG] EXIT_POLICY => ", file=sys.stderr)
print(
    f"POST_TP1_SL_DELAY_BARS={POST_TP1_SL_DELAY_BARS} | "
    f"BE_EPS_ATR_MULT={BE_EPS_ATR_MULT} | "
    f"TRAIL_STYLE={TRAIL_STYLE}",
    file=sys.stderr,
)
print(
    "CHAND="
    f"{{'preTP2': ({CHAND_N_PRE_TP2}, {CHAND_K_PRE_TP2}), "
    f"'postTP2': ({CHAND_N_POST_TP2}, {CHAND_K_POST_TP2}), "
    f"'postTP3': ({CHAND_N_POST_TP3}, {CHAND_K_POST_TP3})}}",
    file=sys.stderr,
)
print(
    "STALL="
    f"{{'bars': {STALL_BARS}, "
    f"'nearATR': {STALL_NEAR_TP_ATR}, "
    f"'rsi': {STALL_RSI_CONFIRM}, "
    f"'eps': {STALL_TP_EPS}}}",
    file=sys.stderr,
)
print("[CONFIG] DATA => ", file=sys.stderr)
print(
    f"OHLCV_TIMEFRAME={OHLCV_TIMEFRAME} | "
    f"FETCH_LIMIT={OHLCV_FETCH_LIMIT} | "
    f"BACKFILL_MIN={OHLCV_BACKFILL_MIN}",
    file=sys.stderr,
)
print("[CONFIG] REENTRY => ", file=sys.stderr)
print(
    f"REQUIRE_NEW_BAR={REQUIRE_NEW_BAR} | REENTRY_COOLDOWN_BARS_5M={REENTRY_COOLDOWN_BARS_5M}",
    file=sys.stderr,
)
print(
    f"BLOCK_REENTRY_PCT={BLOCK_REENTRY_PCT} | MIN_REENTRY_SECONDS={MIN_REENTRY_SECONDS}",
    file=sys.stderr,
)
# Surface potentially conflicting systems so ops can align .env
print("[CONFIG] EFFECTIVE SWITCHES => ", file=sys.stderr)
print(
    f"FLIP_ENABLED={FLIP_ENABLED} (effective={FLIP_EFFECTIVE})",
    file=sys.stderr,
)
print(
    f"DYN_AVOID_ENABLED={DYN_AVOID_ENABLED} (effective={DYN_AVOID_EFFECTIVE})",
    file=sys.stderr,
)
