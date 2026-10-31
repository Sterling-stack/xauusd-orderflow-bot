def should_eject(position_direction, cvd_direction, position_age_seconds):
    """
    PDF rule:
    After 4 seconds, close a position if CVD flips
    against its direction.
    """

    if position_age_seconds < 4:
        return False

    if position_direction == "BUY" and cvd_direction == "BEARISH":
        return True

    if position_direction == "SELL" and cvd_direction == "BULLISH":
        return True

    return False


if __name__ == "__main__":
    print("EARLY EJECT FILTER TEST")
    print("-----------------------")

    test = should_eject("BUY", "BEARISH", 5)

    print("Position: BUY")
    print("CVD: BEARISH")
    print("Position age: 5 seconds")
    print("RESULT:", "EJECT — CLOSE POSITION" if test else "HOLD POSITION")