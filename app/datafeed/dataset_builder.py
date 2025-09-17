from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import List, Optional, cast

try:
    import duckdb
except Exception:
    duckdb = None

try:
    import pandas as pd
except Exception:
    pd = None

try:
    import app.config as C
except Exception:
    import importlib as _importlib

    C = _importlib.import_module("config")


class DatasetBuilder:
    def __init__(self, db_path: Optional[str] = None, root: Optional[str] = None) -> None:
        self.root = Path(str(root or getattr(C, "DATASET_ROOT", "datasets")))
        self.db_path = db_path or getattr(C, "LEDGER_PATH", "ledger.duckdb")
        self.root.mkdir(parents=True, exist_ok=True)
        if duckdb:
            self.con = duckdb.connect(self.db_path)
            self._ensure_schema()
        else:
            self.con = None

    def _ensure_schema(self) -> None:
        assert self.con is not None
        self.con.execute(
            """
        CREATE TABLE IF NOT EXISTS ohlcv(
          ts_ms BIGINT,
          symbol TEXT,
          tf TEXT,
          o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, v DOUBLE
        );
        """
        )

    def backfill_symbol(self, symbol: str, tf: str = "5m", lookback_days: int = 14) -> None:
        # Placeholder: replace with Delta REST client
        now = int(time.time() * 1000)
        step_ms = 5 * 60 * 1000 if tf == "5m" else 60 * 60 * 1000
        n = int((lookback_days * 24 * 60 * 60 * 1000) / step_ms)
        rows = [[float(now - i * step_ms), 0.0, 0.0, 0.0, 0.0, 0.0] for i in range(n)]
        self._ingest(symbol, tf, rows)

    def _ingest(self, symbol: str, tf: str, rows: List[List[float]]) -> None:
        if not rows:
            return
        if self.con is None or pd is None:
            return
        df = pd.DataFrame(rows, columns=["ts_ms", "o", "h", "l", "c", "v"])
        df["symbol"] = symbol
        df["tf"] = tf
        self.con.execute("INSERT INTO ohlcv SELECT * FROM df").df()
        # partition write
        out_dir = self.root / symbol / tf
        out_dir.mkdir(parents=True, exist_ok=True)
        # chunk to daily files
        for day, g in df.groupby(pd.to_datetime(df["ts_ms"], unit="ms").dt.date):
            out = out_dir / f"{symbol}_{tf}_{day}.parquet"
            try:
                g.to_parquet(out, index=False)
            except Exception:
                pass

    def prune_retention(self, days: Optional[int] = None) -> int:
        keep_days = (
            int(days)
            if days is not None
            else int(cast(int, getattr(C, "DATASET_RETENTION_DAYS", 90)))
        )
        cutoff = dt.datetime.utcnow().date() - dt.timedelta(days=keep_days)
        removed = 0
        for sym_dir in self.root.iterdir() if self.root.exists() else []:
            if not sym_dir.is_dir():
                continue
            for tf_dir in sym_dir.iterdir():
                if not tf_dir.is_dir():
                    continue
                for f in tf_dir.glob("*.parquet"):
                    try:
                        parts = f.stem.split("_")
                        day = parts[-1]
                        d = dt.datetime.strptime(day, "%Y-%m-%d").date()
                        if d < cutoff:
                            f.unlink()
                            removed += 1
                    except Exception:
                        continue
        return removed
