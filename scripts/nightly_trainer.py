#!/usr/bin/env python3
from __future__ import annotations

import duckdb


def main() -> None:
    try:
        con = duckdb.connect("market.duckdb")
        n = con.execute("select count(*) from ohlcv").fetchone()[0]
        print(f"[trainer] ohlcv rows: {n}")
    except Exception as e:
        print("[trainer] skipping (no dataset yet):", e)


if __name__ == "__main__":
    main()
