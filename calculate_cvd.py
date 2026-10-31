import duckdb

DB_FILE = "xauusd_ticks.duckdb"

conn = duckdb.connect(DB_FILE)

conn.execute("""
CREATE TABLE IF NOT EXISTS cvd_ticks (
    timestamp_ms BIGINT,
    mid_price DOUBLE,
    tick_delta INTEGER,
    cvd BIGINT
)
""")

conn.execute("DELETE FROM cvd_ticks")

conn.execute("""
INSERT INTO cvd_ticks
WITH price_changes AS (
    SELECT
        timestamp_ms,
        (ask_price + bid_price) / 2.0 AS mid_price,
        LAG((ask_price + bid_price) / 2.0)
            OVER (ORDER BY timestamp_ms) AS previous_mid
    FROM historical_ticks
),
signed_ticks AS (
    SELECT
        timestamp_ms,
        mid_price,
        CASE
            WHEN previous_mid IS NULL THEN 0
            WHEN mid_price > previous_mid THEN 1
            WHEN mid_price < previous_mid THEN -1
            ELSE 0
        END AS tick_delta
    FROM price_changes
)
SELECT
    timestamp_ms,
    mid_price,
    tick_delta,
    SUM(tick_delta) OVER (
        ORDER BY timestamp_ms
        ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
    ) AS cvd
FROM signed_ticks
""")

print("CVD calculation complete.")

print(conn.execute("""
    SELECT timestamp_ms, mid_price, tick_delta, cvd
    FROM cvd_ticks
    ORDER BY timestamp_ms
    LIMIT 10
""").fetchall())

conn.close()