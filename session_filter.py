from datetime import datetime, time
from zoneinfo import ZoneInfo

def session_allowed():
    now = datetime.now(ZoneInfo("America/New_York"))
    current_time = now.time()

    morning_start = time(8, 0)
    morning_end = time(11, 30)

    afternoon_start = time(13, 30)
    afternoon_end = time(16, 0)

    allowed = (
        morning_start <= current_time <= morning_end
        or afternoon_start <= current_time <= afternoon_end
    )

    print("NEW YORK TIME:", now.strftime("%Y-%m-%d %H:%M:%S"))
    print("SESSION STATUS:", "ALLOWED" if allowed else "BLOCKED")

    return allowed


if __name__ == "__main__":
    session_allowed()