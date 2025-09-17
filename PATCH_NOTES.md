# ML + Portfolio Scaffolding Patch (safe-by-default)

- Added:
  - `app/trendscalp_ml_gate.py` (ML hook, OFF unless TS_USE_ML_GATE=true)
  - `app/ml/*` skeletons
  - `app/datafeed/*` skeletons
  - `app/pm/portfolio.py` (portfolio manager stub)
  - `scripts/nightly_trainer.py` (walk-forward trainer skeleton)
- Patched:
  - `app/trendscalp.py`: ML gate hook with meta fields `ml_conf`, `ml_regime`
- No behavior change unless flags enabled. Lint/type format friendly.


## Patch v2
- Added `.env.additions` with portfolio/risk/ML keys (defaults OFF for new logic).
- `app/config.py`: appended safe defaults for new keys.
- `trendscalp.py`:
  - emits `meta.size_mult_suggested` (confidence-scaled sizing suggestion)
  - hooks for ML `ml_conf` history
  - **degrade-tighten** rule (flag `TS_EXIT_DEGRADE_TIGHTEN`)


## Patch v3
- `.env.additions`: dataset (root, retention), ledger (backend/path), Delta API keys, scheduler toggles.
- `app/config.py`: defaults for dataset/ledger/exchange/scheduler.
- `app/ledger_duck.py`: DuckDB ledger helpers with schema + append open/close rows.
- `app/datafeed/dataset_builder.py`: backfill, partitioned parquet writes, retention prune.
- `app/runners/trendscalp_runner.py`: inline **risk gating** before order submit (concurrency, per-trade risk, daily stop).
- `app/scheduler.py`: nightly hooks to dataset autoloader + trainer (flag-driven).


## Patch v4
- `.env.additions`: simulator + delta timeframe keys.
- `app/simulator.py`: CLI dry-run simulator using parquet datasets and TrendScalp engine.
- `app/datafeed/delta_fetcher.py`: structured for official client (placeholder fallback).
- `app/runners/trendscalp_runner.py`:
  - ledger helper functions `_ledger_open/_ledger_close` added
  - TODO markers where to call these in your actual order submit/close blocks
