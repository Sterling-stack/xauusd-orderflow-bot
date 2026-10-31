"""
IMPORT DUKASCOPY TICK CSV FILES INTO THE DATABASE
==================================================
Reads every .csv file in the folder  ticks_csv  (or a folder you name) and
adds the ticks to  historical_ticks  in xauusd_ticks.duckdb.

Expected columns (this is what dukascopy-node writes for tick data):
    timestamp, askPrice, bidPrice, askVolume, bidVolume
The timestamp may be epoch milliseconds or a date-time; both are handled.

Safe to run again: for each file, ticks already in the database inside that
file's time range are replaced, so nothing is duplicated.
Rows with a zero/negative price or ask < bid are skipped and counted.

Run:   python import_ticks.py
       python import_ticks.py C:\\path\\to\\folder
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "xauusd_ticks.duckdb"
NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "DECIMAL", "UBIGINT", "UINTEGER")


def find_column(columns, *names):
    lowered = {c.lower().replace("_", ""): c for c in columns}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


def import_file(con, path):
    quoted = str(path).replace("'", "''")
    described = con.execute(f"DESCRIBE SELECT * FROM read_csv_auto('{quoted}')").fetchall()
    types = {row[0]: row[1].upper() for row in described}
    columns = list(types)

    c_ts = find_column(columns, "timestamp", "time", "datetime", "date")
    c_ask = find_column(columns, "askprice", "ask")
    c_bid = find_column(columns, "bidprice", "bid")
    c_askv = find_column(columns, "askvolume", "askvol")
    c_bidv = find_column(columns, "bidvolume", "bidvol")
    if not (c_ts and c_ask and c_bid):
        print(f"  SKIPPED {path.name}: need timestamp, askPrice and bidPrice columns. Found: {columns}")
        return 0, 0

    if types[c_ts].startswith(NUMERIC):
        ts_expr = f'CAST("{c_ts}" AS BIGINT)'
    else:
        ts_expr = f'CAST(round(epoch("{c_ts}") * 1000) AS BIGINT)'
    askv = f'CAST("{c_askv}" AS DOUBLE)' if c_askv else "CAST(0 AS DOUBLE)"
    bidv = f'CAST("{c_bidv}" AS DOUBLE)' if c_bidv else "CAST(0 AS DOUBLE)"

    source = f"""
        SELECT {ts_expr} AS timestamp_ms,
               CAST("{c_ask}" AS DOUBLE) AS ask_price,
               CAST("{c_bid}" AS DOUBLE) AS bid_price,
               {askv} AS ask_volume,
               {bidv} AS bid_volume
        FROM read_csv_auto('{quoted}')
    """
    total, lo, hi = con.execute(f"SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM ({source})").fetchone()
    if not total:
        print(f"  {path.name}: empty file")
        return 0, 0

    con.execute("DELETE FROM historical_ticks WHERE timestamp_ms BETWEEN ? AND ?", [lo, hi])
    con.execute(f"""
        INSERT INTO historical_ticks
        SELECT timestamp_ms, to_timestamp(timestamp_ms / 1000.0), ask_price, bid_price, ask_volume, bid_volume
        FROM ({source})
        WHERE ask_price > 0 AND bid_price > 0 AND ask_price >= bid_price
        ORDER BY timestamp_ms
    """)
    good = con.execute("SELECT COUNT(*) FROM historical_ticks WHERE timestamp_ms BETWEEN ? AND ?", [lo, hi]).fetchone()[0]
    return good, total - good


def main():
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE_DIR / "ticks_csv"
    files = sorted(folder.glob("*.csv"))
    if not files:
        print(f"No .csv files found in {folder}")
        print("Download ticks with dukascopy-node first (see the steps I gave you), then put the CSVs there.")
        return 1

    con = duckdb.connect(str(DB_PATH))
    con.execute("""
        CREATE TABLE IF NOT EXISTS historical_ticks (
            timestamp_ms BIGINT,
            timestamp_utc TIMESTAMP WITH TIME ZONE,
            ask_price DOUBLE,
            bid_price DOUBLE,
            ask_volume DOUBLE,
            bid_volume DOUBLE
        )
    """)

    grand_good = grand_bad = 0
    for path in files:
        print(f"Importing {path.name} ...", flush=True)
        good, bad = import_file(con, path)
        grand_good += good
        grand_bad += bad
        print(f"  ticks stored: {good:,}   skipped as invalid: {bad:,}")

    con.execute("CHECKPOINT")
    n, lo, hi = con.execute("SELECT COUNT(*), MIN(timestamp_ms), MAX(timestamp_ms) FROM historical_ticks").fetchone()
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    print("\nDONE")
    print(f"Database now holds {n:,} ticks from {fmt(lo)} to {fmt(hi)} (UTC).")
    print(f"This run: {grand_good:,} stored, {grand_bad:,} invalid rows skipped.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
