import duckdb

DB_PATH = "xauusd_ticks.duckdb"
THRESHOLD_MULTIPLIER = 1.20
WINDOW_MS = 5 * 60 * 1000

conn = duckdb.connect(DB_PATH)

latest_timestamp = conn.execute("""
    SELECT MAX(timestamp_ms)
    FROM historical_ticks
""").fetchone()[0]

window_start = latest_timestamp - WINDOW_MS

result = conn.execute("""
    WITH recent_spreads AS (
        SELECT
            timestamp_ms,
            ask_price - bid_price AS spread
        FROM historical_ticks
        WHERE timestamp_ms >= ?
          AND timestamp_ms <= ?
    )
    SELECT
        COUNT(*),
        AVG(spread),
        MAX(timestamp_ms)
    FROM recent_spreads
""", [window_start, latest_timestamp]).fetchone()

tick_count, average_spread, _ = result

current_tick = conn.execute("""
    SELECT
        timestamp_ms,
        bid_price,
        ask_price,
        ask_price - bid_price AS spread
    FROM historical_ticks
    ORDER BY timestamp_ms DESC
    LIMIT 1
""").fetchone()

timestamp, bid, ask, current_spread = current_tick

print("DYNAMIC SPREAD GUARD — HISTORICAL TEST")
print("--------------------------------------")
print("Ticks in previous 5 minutes:", tick_count)
print(f"Average spread: {average_spread:.5f}")
print(f"Latest spread:  {current_spread:.5f}")

if average_spread is None or tick_count == 0:
    print("RESULT: REJECT — insufficient spread data")
else:
    spread_limit = average_spread * THRESHOLD_MULTIPLIER
    print(f"Spread limit (average x 1.20): {spread_limit:.5f}")

    if current_spread > spread_limit:
        print("RESULT: REJECT — spread too wide")
    else:
        print("RESULT: PASS — spread within limit")

print("\nHistorical-data test only; no trades placed.")

conn.close()