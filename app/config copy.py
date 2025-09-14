# app/config.py
from dotenv import load_dotenv
import os

load_dotenv()

def _req(k: str) -> str:
    v = os.getenv(k)
    if not v:
        raise RuntimeError(f"Missing env: {k}")
    return v

def _bool(s: str) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "y", "on")

def _floats(s: str):
    return [float(x.strip()) for x in s.split(",") if x.strip()]

# ===== Core =====
PAIR            = _req("PAIR")
EXCHANGE_ID     = _req("EXCHANGE_ID")
DRY_RUN         = _bool(_req("DRY_RUN"))
MAX_LEVERAGE    = int(_req("MAX_LEVERAGE"))

# ===== Sizing / Risk =====
SIZING_MODE         = _req("SIZING_MODE")                    # capital_frac | risk_r | both
CAPITAL_FRACTION    = float(_req("CAPITAL_FRACTION"))
RISK_PCT            = float(_req("RISK_PCT"))
RSI_OB              = int(_req("RSI_OB"))
RSI_OS              = int(_req("RSI_OS"))
DB_PATH             = _req("TASER_DB")

# ===== OpenAI / Audit =====
OPENAI_API_KEY          = _req("OPENAI_API_KEY")
OPENAI_USE              = os.getenv("OPENAI_USE", "false").lower() == "true"
AUDIT_DEBOUNCE_SECONDS  = int(os.getenv("AUDIT_DEBOUNCE_SECONDS", "15"))
AUDIT_CACHE_TTL_SECONDS = int(os.getenv("AUDIT_CACHE_TTL_SECONDS", "300"))
AUDIT_SAMPLE_TAIL_N     = int(os.getenv("AUDIT_SAMPLE_TAIL_N", "30"))
AUDIT_MAX_LEVELS        = int(os.getenv("AUDIT_MAX_LEVELS", "12"))

# ===== Dynamic Avoid / Confluence =====
DYN_AVOID_ENABLED= os.getenv("DYN_AVOID_ENABLED", "true").lower() in ("1","true","yes")
AVOID_LOOKBACK_BARS = int(_req("AVOID_LOOKBACK_BARS"))
CHOP_MIN_FLIPS      = int(_req("CHOP_MIN_FLIPS"))
CHOP_MAX_WIDTH_PCT  = float(_req("CHOP_MAX_WIDTH_PCT"))
CONF_MAX_SPREAD_PCT = float(_req("CONF_MAX_SPREAD_PCT"))

# ===== Scalper (runtime exits) =====
SCALP_ENABLED           = _bool(_req("SCALP_ENABLED"))
SCALP_MIN_TICK_PROFIT   = float(_req("SCALP_MIN_TICK_PROFIT"))
SCALP_MAX_TICK_PROFIT   = float(_req("SCALP_MAX_TICK_PROFIT"))
SCALP_TIMEFRAME         = _req("SCALP_TIMEFRAME")
SCALP_MAX_HOLD_SECONDS  = int(_req("SCALP_MAX_HOLD_SECONDS"))

# ===== Scheduler / Scan pacing =====
SCAN_INTERVAL_SECONDS = int(_req("SCAN_INTERVAL_SECONDS"))

# ===== Exchange keys / endpoints =====
DELTA_API_KEY  = os.getenv("DELTA_API_KEY", "").strip()
DELTA_API_SECRET = os.getenv("DELTA_API_SECRET", "").strip()
DELTA_BASE_URL = os.getenv("DELTA_BASE_URL", "").strip()
DELTA_WS_URL   = os.getenv("DELTA_WS_URL", "").strip()

# ===== Telegram =====
TG_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ===== Entry windows / thresholds =====
NEAR_PDH_PCT            = float(_req("NEAR_PDH_PCT"))
NEAR_AVWAP_PCT          = float(_req("NEAR_AVWAP_PCT"))
NEAR_VWAP_PCT_MIN       = float(_req("NEAR_VWAP_PCT_MIN"))
NEAR_VWAP_PCT_MAX       = float(_req("NEAR_VWAP_PCT_MAX"))
ATR_NEAR_MULT           = float(_req("ATR_NEAR_MULT"))
VWAP_RECLAIM_ATR_MULT   = float(_req("VWAP_RECLAIM_ATR_MULT"))
AVWAP_RECLAIM_ATR_MULT  = float(_req("AVWAP_RECLAIM_ATR_MULT"))
AGGRESSION       = os.getenv("AGGRESSION", "balanced").strip().lower()   # conservative | balanced | aggressive

# ===== Single-position / hygiene =====
SINGLE_POSITION_MODE = os.getenv("SINGLE_POSITION_MODE", "true").lower() == "true"
MANAGE_POLL_SECONDS  = int(os.getenv("MANAGE_POLL_SECONDS", "3"))
FAST_SCAN_AFTER_TP1  = int(os.getenv("FAST_SCAN_AFTER_TP1", "2"))
STATUS_INTERVAL_SECONDS = int(os.getenv("STATUS_INTERVAL_SECONDS", "60"))

TP1_LOCK_MODE   = os.getenv("TP1_LOCK_MODE", "tp1_exact").strip()
TP1_LOCK_OFFSET = float(os.getenv("TP1_LOCK_OFFSET", "0.10"))
TP2_LOCK_MODE   = os.getenv("TP2_LOCK_MODE", "tp2_exact").strip()
TP2_LOCK_OFFSET = float(os.getenv("TP2_LOCK_OFFSET", "0.12"))

PLACE_TP3_LIMIT    = os.getenv("PLACE_TP3_LIMIT", "true").lower() == "true"
DYNAMIC_TP_EXTEND  = os.getenv("DYNAMIC_TP_EXTEND", "true").lower() == "true"

# Re-entry controls
MIN_REENTRY_SECONDS = int(os.getenv("MIN_REENTRY_SECONDS", "60"))
BLOCK_REENTRY_PCT   = float(os.getenv("BLOCK_REENTRY_PCT", "0.003"))
REQUIRE_NEW_BAR     = os.getenv("REQUIRE_NEW_BAR", "true").lower() == "true"

# ===== Heatmap retention (optional) =====
HEATMAP_RETENTION_DAYS = int(os.getenv("HEATMAP_RETENTION_DAYS", "90"))





# When heatmap wall tightening is applied, pad SL by this fraction of ATR
HEATMAP_SL_PAD_MULT = float(os.getenv("HEATMAP_SL_PAD_MULT", "0.75"))

# Optional: rule guardrails exposed explicitly (were implicit defaults before)
PERSIST_BARS = int(os.getenv("PERSIST_BARS", "2"))   # bars of persistence confirmation




# add near the other TP lock/env loads
TP_LOCK_CONFIRM_BARS = int(os.getenv("TP_LOCK_CONFIRM_BARS", "2"))

TP1_LOCK_MODE   = os.getenv("TP1_LOCK_MODE", "breakeven_buffer").strip()
TP2_LOCK_MODE   = os.getenv("TP2_LOCK_MODE", "tp2_buffer").strip()

TP1_LOCK_BUFFER_ATR = float(os.getenv("TP1_LOCK_BUFFER_ATR", "0.35"))
TP2_LOCK_BUFFER_ATR = float(os.getenv("TP2_LOCK_BUFFER_ATR", "0.25"))

BE_BUFFER_PCT = float(os.getenv("BE_BUFFER_PCT", "0.0015"))


TP_LOCK_ATR_MULT=float(os.getenv("TP_LOCK_ATR_MULT","0.90"))
TP_LOCK_ABS_MIN = float(os.getenv("TP_LOCK_ABS_MIN", "0.18"))
HEATMAP_TRAIL_GRACE_SEC = float(os.getenv("HEATMAP_TRAIL_GRACE_SEC", "150"))


# Extra padding around structure for initial SL (multiples of ATR)
ATR_SL_MULT = float(os.getenv("ATR_SL_MULT", "0.70"))
STDEV_SL_MULT    = float(os.getenv("STDEV_SL_MULT", 2.20))
MIN_R_MULT       = float(os.getenv("MIN_R_MULT", 1.4))

TP_MODE           = os.getenv("TP_MODE", "atr").strip().lower()
TP1_ATR_MULT      = float(os.getenv("TP1_ATR_MULT", "0.9"))
TP2_ATR_MULT      = float(os.getenv("TP2_ATR_MULT", "1.5"))
TP3_ATR_MULT      = float(os.getenv("TP3_ATR_MULT", "2.2"))

# --- Volatility-aware SL/TP defaults ---
MICRO_N_1M       = int(os.getenv("MICRO_N_1M", 30))
MICRO_Q_1M       = float(os.getenv("MICRO_Q_1M", 0.80))
MICRO_K_5M       = int(os.getenv("MICRO_K_5M", 6))
MICRO_EWMA_ALPHA = float(os.getenv("MICRO_EWMA_ALPHA", 0.35))
MICRO_TR1M_MULT  = float(os.getenv("MICRO_TR1M_MULT", 1.0))
MICRO_TR5M_MULT  = float(os.getenv("MICRO_TR5M_MULT", 0.45))

MIN_SL_PCT       = float(os.getenv("MIN_SL_PCT", 0.0040))   # 0.25%
MAX_SL_PCT       = float(os.getenv("MAX_SL_PCT", 0.012))    # 1.5%
# --- TP Locking / Grace Behavior ---
TP_LOCK_STYLE        = os.getenv("TP_LOCK_STYLE", "trail_fracR").strip()  # "trail_fracR" or "to_tp1"
TP1_LOCK_FRACR       = float(os.getenv("TP1_LOCK_FRACR", 0.40))           # fraction of R to trail after TP1
TP2_LOCK_FRACR       = float(os.getenv("TP2_LOCK_FRACR", 0.75))           # fraction of R to trail after TP2
TP1_LOCK_ATR_MULT    = float(os.getenv("TP1_LOCK_ATR_MULT", 0.25))        # ATR cushion multiplier for TP1
TP2_LOCK_ATR_MULT    = float(os.getenv("TP2_LOCK_ATR_MULT", 0.35))        # ATR cushion multiplier for TP2
TP_LOCK_GRACE_SEC    = int(os.getenv("TP_LOCK_GRACE_SEC", 3))             # seconds grace before first lock
INVALIDATION_GRACE_SEC = int(os.getenv("INVALIDATION_GRACE_SEC", 8))      # seconds grace before invalidation allowed


# ===== New: SL robustness knobs (defaults are safe if .env not yet updated) =====
# Absolute minimum SL distance from entry (e.g., 0.0045 = 0.45%)
MIN_SL_PCT = float(os.getenv("MIN_SL_PCT", "0.0030"))
MAX_SL_PCT = float(os.getenv("MAX_SL_PCT", "0.0120"))
FEE_PCT    = float(os.getenv("FEE_PCT", 0.0005))                # 0.05% fee cushion
FEE_PAD_MULT = float(os.getenv("FEE_PAD_MULT", 2.0))
TP_R_MULTIS = [float(x) for x in os.getenv("TP_R_MULTIS", "1.0,1.8,2.6").split(",")]
SL_MIX_ALPHA     = float(os.getenv("SL_MIX_ALPHA", 0.65))
SL_ATR_MULT      = float(os.getenv("SL_ATR_MULT", 0.95))
SL_MIX_ALPHA    = float(os.getenv("SL_MIX_ALPHA", 0.55))
SL_NOISE_MULT    = float(os.getenv("SL_NOISE_MULT", 1.90))
SL_NOISE_BARS_1M = int(os.getenv("SL_NOISE_BARS_1M", 10))

# Invalidation hysteresis
INVALIDATION_MODE           = os.getenv("INVALIDATION_MODE","hysteresis").strip()
INVALIDATE_MIN_HOLD_SEC     = int(os.getenv("INVALIDATE_MIN_HOLD_SEC","45"))
INVALIDATE_REQUIRE_BARS     = int(os.getenv("INVALIDATE_REQUIRE_BARS","2"))
INVALIDATE_PRICE_DRIFT_PCT  = float(os.getenv("INVALIDATE_PRICE_DRIFT_PCT","0.0015"))
INVALIDATE_REQUIRE_OPPOSITE = os.getenv("INVALIDATE_REQUIRE_OPPOSITE","true").lower()=="true"
INVALIDATE_USE_TREND        = os.getenv("INVALIDATE_USE_TREND","true").lower()=="true"





    