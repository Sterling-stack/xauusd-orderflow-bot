import duckdb

conn = duckdb.connect("xauusd_ticks.duckdb")

rows = conn.execute("""
    SELECT cvd
    FROM ticks_15m
    ORDER BY interval_start_ms DESC
    LIMIT 4
""").fetchall()

conn.close()

print("15-MINUTE CVD SLOPE")
print("-------------------")

if len(rows) < 4:
    print("Not enough 15-minute periods.")
else:
    # Put the four CVD values in chronological order.
    cvd_values = [row[0] for row in reversed(rows)]

    # Linear regression slope across periods 0, 1, 2, 3.
    x_values = [0, 1, 2, 3]
    x_mean = sum(x_values) / 4
    y_mean = sum(cvd_values) / 4

    numerator = sum(
        (x - x_mean) * (y - y_mean)
        for x, y in zip(x_values, cvd_values)
    )
    denominator = sum(
        (x - x_mean) ** 2 for x in x_values
    )

    slope = numerator / denominator

    print("Last four CVD values:", cvd_values)
    print("Linear regression slope:", round(slope, 4))

    if slope > 0:
        print("15-minute bias: BULLISH")
        print("PDF rule: Disable 5-minute SELL signals.")
    elif slope < 0:
        print("15-minute bias: BEARISH")
        print("PDF rule: Disable 5-minute BUY signals.")
    else:
        print("15-minute bias: FLAT")
        print("No directional bias from this slope.")

print("\nHistorical-data test only; not a live trading signal.")