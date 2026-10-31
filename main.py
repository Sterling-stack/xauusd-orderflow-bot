
import duckdb
from pathlib import Path
from datetime import datetime, time
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "xauusd_ticks.duckdb"


def check_database():
    if not DB_PATH.exists():
        print("ERROR: Database file not found.")
        return False

    connection = duckdb.connect(str(DB_PATH), read_only=True)
    tables = connection.execute("SHOW TABLES").fetchall()
    connection.close()

    print("DATABASE CONNECTION: OK")
    print("TABLES FOUND:", len(tables))
    return True


def check_mtf_cvd_slope():
    connection = duckdb.connect(str(DB_PATH), read_only=True)

    rows = connection.execute("""
        SELECT cvd
        FROM ticks_15m
        ORDER BY interval_start_ms DESC
        LIMIT 4
    """).fetchall()

    connection.close()

    if len(rows) < 4:
        print("MTF CVD: NOT ENOUGH DATA")
        return None

    values = [row[0] for row in reversed(rows)]
    x = [0, 1, 2, 3]
    mean_x = sum(x) / 4
    mean_y = sum(values) / 4

    denominator = sum((value - mean_x) ** 2 for value in x)

    slope = sum(
        (x[i] - mean_x) * (values[i] - mean_y)
        for i in range(4)
    ) / denominator

    bias = (
        "BULLISH" if slope > 0
        else "BEARISH" if slope < 0
        else "FLAT"
    )

    print("\n15-MINUTE CVD SLOPE")
    print("LAST FOUR CVD VALUES:", values)
    print("SLOPE:", slope)
    print("BIAS:", bias)

    return bias


def check_dynamic_spread():
    connection = duckdb.connect(str(DB_PATH), read_only=True)

    latest_timestamp = connection.execute("""
        SELECT MAX(timestamp_ms)
        FROM historical_ticks
    """).fetchone()[0]

    if latest_timestamp is None:
        connection.close()
        print("\nSPREAD GUARD: NO DATA")
        return False

    window_start = latest_timestamp - (5 * 60 * 1000)

    average_spread, tick_count = connection.execute("""
        SELECT AVG(ask_price - bid_price), COUNT(*)
        FROM historical_ticks
        WHERE timestamp_ms >= ?
          AND timestamp_ms <= ?
    """, [window_start, latest_timestamp]).fetchone()

    current_spread = connection.execute("""
        SELECT ask_price - bid_price
        FROM historical_ticks
        ORDER BY timestamp_ms DESC
        LIMIT 1
    """).fetchone()[0]

    connection.close()

    if average_spread is None or tick_count == 0:
        print("\nSPREAD GUARD: REJECT — insufficient data")
        return False

    spread_limit = average_spread * 1.20
    allowed = current_spread <= spread_limit

    print("\nDYNAMIC SPREAD GUARD")
    print("TICKS CHECKED:", tick_count)
    print("AVERAGE SPREAD:", round(average_spread, 5))
    print("CURRENT SPREAD:", round(current_spread, 5))
    print("SPREAD LIMIT:", round(spread_limit, 5))
    print("RESULT:", "PASS" if allowed else "REJECT")

    return allowed


def check_cvd_velocity():
    connection = duckdb.connect(str(DB_PATH), read_only=True)

    latest_time = connection.execute("""
        SELECT MAX(timestamp_ms)
        FROM cvd_ticks
    """).fetchone()[0]

    if latest_time is None:
        connection.close()
        print("\nCVD VELOCITY: NO DATA")
        return None

    def get_cvd_at_or_before(target_time):
        result = connection.execute("""
            SELECT cvd
            FROM cvd_ticks
            WHERE timestamp_ms <= ?
            ORDER BY timestamp_ms DESC
            LIMIT 1
        """, [target_time]).fetchone()

        return result[0] if result else None

    current_cvd = get_cvd_at_or_before(latest_time)
    cvd_15s_ago = get_cvd_at_or_before(latest_time - 15000)
    cvd_30s_ago = get_cvd_at_or_before(latest_time - 30000)

    connection.close()

    print("\nCVD VELOCITY COMPARISON")

    if any(value is None for value in [
        current_cvd, cvd_15s_ago, cvd_30s_ago
    ]):
        print("NOT ENOUGH HISTORICAL DATA")
        return None

    latest_change = current_cvd - cvd_15s_ago
    previous_change = cvd_15s_ago - cvd_30s_ago

    print("LATEST 15-SECOND CHANGE:", latest_change)
    print("PREVIOUS 15-SECOND CHANGE:", previous_change)

    if latest_change > previous_change:
        result = "INCREASING"
    elif latest_change < previous_change:
        result = "DECREASING"
    else:
        result = "UNCHANGED"

    print("CVD MOVEMENT:", result)
    print("NOTE: Tick-direction counts, not traded volume.")

    return result


def check_session():
    now = datetime.now(ZoneInfo("America/New_York"))
    current_time = now.time()

    allowed = (
        time(8, 0) <= current_time <= time(11, 30)
        or time(13, 30) <= current_time <= time(16, 0)
    )

    print("\nSESSION FILTER")
    print("NEW YORK TIME:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("SESSION STATUS:", "ALLOWED" if allowed else "BLOCKED")

    return allowed


def test_early_eject():
    def should_eject(position_direction, cvd_direction, position_age_seconds):
        if position_age_seconds < 4:
            return False

        if position_direction == "BUY" and cvd_direction == "BEARISH":
            return True

        if position_direction == "SELL" and cvd_direction == "BULLISH":
            return True

        return False

    print("\nEARLY EJECT FILTER TEST")

    test = should_eject("BUY", "BEARISH", 5)

    print("POSITION: BUY")
    print("CVD DIRECTION: BEARISH")
    print("POSITION AGE: 5 SECONDS")
    print("RESULT:", "EJECT — CLOSE POSITION" if test else "HOLD POSITION")

    return test


if __name__ == "__main__":
    print("XAUUSD ORDER FLOW BOT — FILTER CHECKS")
    print("-------------------------------------")

    if check_database():
        check_mtf_cvd_slope()
        check_dynamic_spread()
        check_cvd_velocity()
        check_session()
        test_early_eject()

    print("\nHISTORICAL AND LOGIC TESTS ONLY — NO TRADES PLACED.")
