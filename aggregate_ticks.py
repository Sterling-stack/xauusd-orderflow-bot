import duckdb

conn = duckdb.connect("xauusd_ticks.duckdb")

for minutes in (5, 15):
    table_name = f"ticks_{minutes}m"

    conn.execute(f"""
        CREATE OR REPLACE TABLE {table_name} AS
        SELECT
            CAST(FLOOR(timestamp_ms / {minutes * 60 * 1000})
                AS BIGINT) * {minutes * 60 * 1000} AS interval_start_ms,
            FIRST(mid_price ORDER BY timestamp_ms) AS open_price,
            MAX(mid_price) AS high_price,
            MIN(mid_price) AS low_price,
            LAST(mid_price ORDER BY timestamp_ms) AS close_price,
            SUM(tick_delta) AS volume_delta,
            LAST(cvd ORDER BY timestamp_ms) AS cvd
        FROM cvd_ticks
        GROUP BY FLOOR(timestamp_ms / {minutes * 60 * 1000})
        ORDER BY interval_start_ms
    """)

    count = conn.execute(
        f"SELECT COUNT(*) FROM {table_name}"
    ).fetchone()[0]

    print(f"{minutes}-minute intervals created: {count}")

    print(conn.execute(f"""
        SELECT interval_start_ms, open_price, high_price,
               low_price, close_price, volume_delta, cvd
        FROM {table_name}
        ORDER BY interval_start_ms
        LIMIT 3
    """).fetchall())

conn.close()
print("Aggregation complete.")