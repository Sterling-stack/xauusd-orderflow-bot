import duckdb

conn = duckdb.connect("xauusd_ticks.duckdb")

latest_time = conn.execute(
    "SELECT MAX(timestamp_ms) FROM cvd_ticks"
).fetchone()[0]

def get_cvd_at_or_before(target_time):
    result = conn.execute(
        """
        SELECT cvd
        FROM cvd_ticks
        WHERE timestamp_ms <= ?
        ORDER BY timestamp_ms DESC
        LIMIT 1
        """,
        [target_time]
    ).fetchone()

    return result[0] if result else None

current_cvd = get_cvd_at_or_before(latest_time)
cvd_15s_ago = get_cvd_at_or_before(latest_time - 15000)
cvd_30s_ago = get_cvd_at_or_before(latest_time - 30000)

print("CVD VELOCITY COMPARISON")
print("-----------------------")

if all(v is not None for v in [
    current_cvd, cvd_15s_ago, cvd_30s_ago
]):
    latest_change = current_cvd - cvd_15s_ago
    previous_change = cvd_15s_ago - cvd_30s_ago

    print("Latest 15-second change:", latest_change)
    print("Previous 15-second change:", previous_change)

    if latest_change > previous_change:
        print("CVD change is increasing.")
    elif latest_change < previous_change:
        print("CVD change is decreasing.")
    else:
        print("CVD change is unchanged.")

    print("Note: Tick-direction counts, not traded volume.")
else:
    print("Not enough historical data.")

conn.close()