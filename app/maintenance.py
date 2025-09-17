#!/usr/bin/env python3
import argparse
import gzip
import os
import shutil
import sqlite3
from datetime import datetime, timedelta


def purge_sqlite(db_path: str, table: str, ts_column: str, keep_days: int):
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        # Normalize table/column names via quoting
        cutoff_ts = datetime.utcnow() - timedelta(days=keep_days)
        # If your ts column is stored as ISO text, this works; if epoch, switch to integer compare.
        cutoff_iso = cutoff_ts.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[DB] Purging rows in {table} older than {cutoff_iso} (UTC) ...")
        cur.execute(f'DELETE FROM "{table}" WHERE "{ts_column}" < ?', (cutoff_iso,))
        print(f"[DB] Rows deleted: {cur.rowcount}")
        conn.commit()
        print("[DB] Running VACUUM to reclaim space ...")
        cur.execute("VACUUM")
        conn.commit()
        print("[DB] Done.")
    finally:
        conn.close()


def rotate_csv(csv_path: str, keep_lines: int):
    if not os.path.exists(csv_path):
        print(f"[CSV] {csv_path} not found; skipping.")
        return

    # Count lines
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        total = sum(1 for _ in f)
    print(f"[CSV] {csv_path}: {total} lines. Keep last {keep_lines}.")

    if total <= keep_lines:
        print("[CSV] No rotation needed.")
        return

    # Create a temp with only the tail
    tmp_tail = csv_path + ".tail"
    # Efficient tail-read
    with open(csv_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        # Read chunks from end until enough newlines are found
        chunk = 64 * 1024
        data = bytearray()
        newlines = 0
        pos = end
        while pos > 0 and newlines <= keep_lines:
            read_size = chunk if pos - chunk > 0 else pos
            pos -= read_size
            f.seek(pos)
            block = f.read(read_size)
            data[:0] = block
            newlines = data.count(b"\n")
        # Keep only last keep_lines lines
        lines = data.splitlines()[-keep_lines:]
    with open(tmp_tail, "wb") as out:
        out.write(b"\n".join(lines) + b"\n")

    # Archive the original
    stamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    gz_path = f"{os.path.splitext(csv_path)[0]}_{stamp}.csv.gz"
    with open(csv_path, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    print(f"[CSV] Archived old file to {gz_path}")

    # Replace original with tail
    shutil.move(tmp_tail, csv_path)
    print(f"[CSV] Rotated. Kept last {keep_lines} lines.")


def main():
    ap = argparse.ArgumentParser(description="Purge SQLite telemetry and rotate CSV.")
    ap.add_argument("--db", required=True, help="Path to SQLite DB (e.g., taser.db)")
    ap.add_argument("--table", default="telemetry", help="Telemetry table name")
    ap.add_argument("--ts-column", default="ts", help="Timestamp column (ISO text)")
    ap.add_argument("--keep-days", type=int, default=7, help="Days to retain")
    ap.add_argument("--csv", help="Path to telemetry CSV to rotate")
    ap.add_argument("--keep-lines", type=int, default=150000, help="Lines to retain in CSV")
    args = ap.parse_args()

    purge_sqlite(args.db, args.table, args.ts_column, args.keep_days)
    if args.csv:
        rotate_csv(args.csv, args.keep_lines)


if __name__ == "__main__":
    main()
