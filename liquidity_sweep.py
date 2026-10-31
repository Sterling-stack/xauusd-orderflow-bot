
import duckdb
from bisect import bisect_right

DB_NAME = "xauusd_ticks.duckdb"

CANDLE_MS = 15 * 60 * 1000
RECLAIM_WINDOW_MS = 30 * 1000


def test_liquidity_sweeps():
    con = duckdb.connect(DB_NAME)

    candles = con.execute("""
        SELECT interval_start_ms, high_price, low_price
        FROM ticks_15m
        ORDER BY interval_start_ms ASC
    """).fetchall()

    if len(candles) < 2:
        print("Not enough 15-minute candles.")
        con.close()
        return

    starts = [row[0] for row in candles]

    states = {}
    active_events = []

    reclaimed = 0
    failed = 0
    pending = 0
    high_events = 0
    low_events = 0

    last_tick_timestamp = None

    ticks = con.execute("""
        SELECT timestamp_ms, bid_price, ask_price
        FROM historical_ticks
        ORDER BY timestamp_ms ASC
    """).fetchall()

    for timestamp, bid, ask in ticks:
        last_tick_timestamp = timestamp

        # First resolve previously active events.
        remaining_events = []

        for event in active_events:
            if timestamp > event["deadline"]:
                failed += 1
                continue

            if event["direction"] == "HIGH" and bid <= event["level"]:
                reclaimed += 1
                continue

            if event["direction"] == "LOW" and ask >= event["level"]:
                reclaimed += 1
                continue

            remaining_events.append(event)

        active_events = remaining_events

        # Find the 15-minute candle containing this tick.
        candle_index = bisect_right(starts, timestamp) - 1

        if candle_index < 1:
            continue

        candle_start = starts[candle_index]

        if timestamp >= candle_start + CANDLE_MS:
            continue

        previous_high = candles[candle_index - 1][1]
        previous_low = candles[candle_index - 1][2]

        if candle_index not in states:
            states[candle_index] = {
                "high_armed": True,
                "low_armed": True,
                "high_active": False,
                "low_active": False
            }

        state = states[candle_index]

        # Rearm only after price has returned inside the reference level.
        if bid <= previous_high:
            state["high_armed"] = True

        if ask >= previous_low:
            state["low_armed"] = True

        # Detect a high-side liquidity sweep.
        if (
            state["high_armed"]
            and not state["high_active"]
            and bid > previous_high
        ):
            active_events.append({
                "direction": "HIGH",
                "level": previous_high,
                "deadline": timestamp + RECLAIM_WINDOW_MS
            })

            state["high_armed"] = False
            state["high_active"] = True
            high_events += 1

        # Detect a low-side liquidity sweep.
        if (
            state["low_armed"]
            and not state["low_active"]
            and ask < previous_low
        ):
            active_events.append({
                "direction": "LOW",
                "level": previous_low,
                "deadline": timestamp + RECLAIM_WINDOW_MS
            })

            state["low_armed"] = False
            state["low_active"] = True
            low_events += 1

        # Release the active flag after its event is resolved.
        # The armed flag still requires a return inside the level
        # before another break can be detected.

        active_directions = {
            (event["direction"], event["level"])
            for event in active_events
        }

        # Keep each candle's active state independent.
        for index, candle_state in states.items():
            high_level = candles[index - 1][1]
            low_level = candles[index - 1][2]

            candle_state["high_active"] = any(
                event["direction"] == "HIGH"
                and event["level"] == high_level
                for event in active_events
            )

            candle_state["low_active"] = any(
                event["direction"] == "LOW"
                and event["level"] == low_level
                for event in active_events
            )

    # Classify events remaining when historical data ends.
    for event in active_events:
        if last_tick_timestamp > event["deadline"]:
            failed += 1
        else:
            pending += 1

    con.close()

    completed = reclaimed + failed

    print()
    print("LIQUIDITY SWEEP / RECLAIM TEST")
    print("--------------------------------")
    print("15-minute candles:", len(candles))
    print("Historical ticks:", len(ticks))
    print("High-side events:", high_events)
    print("Low-side events:", low_events)
    print("Total detected events:", high_events + low_events)
    print("Completed events:", completed)
    print("Reclaimed within 30 seconds:", reclaimed)
    print("Not reclaimed:", failed)
    print("Pending at data end:", pending)

    if completed:
        rate = reclaimed / completed * 100
        print(f"Reclaim rate: {rate:.2f}%")
    else:
        print("Reclaim rate: Not available")

    print("Absorption: NOT VERIFIED")
    print("Historical test only — no trades placed.")


if __name__ == "__main__":
    test_liquidity_sweeps()
