
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5


def main():
    print("EXNESS MT5 DATA CHECK")
    print("This script does not place or modify trades.")
    print()

    if not mt5.initialize():
        print("MT5 connection failed:", mt5.last_error())
        print("Open MT5, log in to Exness, and try again.")
        return

    try:
        print("MT5 terminal version:", mt5.version())

        terminal = mt5.terminal_info()
        if terminal is not None:
            print("Terminal connected:", terminal.connected)

        symbols = mt5.symbols_get()
        if symbols is None:
            print("Could not retrieve symbols:", mt5.last_error())
            return

        gold_symbols = [
            s.name for s in symbols
            if "XAU" in s.name.upper() or "GOLD" in s.name.upper()
        ]

        print("Gold symbols found:", gold_symbols)

        if not gold_symbols:
            print("No gold symbol found. Check Market Watch in MT5.")
            return

        for symbol in gold_symbols[:8]:
            print("\n---", symbol, "---")

            info = mt5.symbol_info(symbol)
            if info is None:
                print("Symbol information unavailable.")
                continue

            if not info.visible and not mt5.symbol_select(symbol, True):
                print("Could not select symbol:", mt5.last_error())
                continue

            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                print("Current quote unavailable:", mt5.last_error())
            else:
                print("Bid:", tick.bid)
                print("Ask:", tick.ask)
                print("Latest quote timestamp (ms):", tick.time_msc)

            start = datetime.now(timezone.utc) - timedelta(minutes=5)
            ticks = mt5.copy_ticks_from(
                symbol, start, 1000, mt5.COPY_TICKS_ALL
            )

            if ticks is None:
                print("Recent tick query failed:", mt5.last_error())
            else:
                print("Ticks returned for recent window:", len(ticks))
                if len(ticks):
                    first_ms = int(ticks[0]["time_msc"])
                    last_ms = int(ticks[-1]["time_msc"])
                    print(
                        "First returned tick (UTC):",
                        datetime.fromtimestamp(
                            first_ms / 1000, timezone.utc
                        ).isoformat()
                    )
                    print(
                        "Last returned tick (UTC):",
                        datetime.fromtimestamp(
                            last_ms / 1000, timezone.utc
                        ).isoformat()
                    )

            subscribed = mt5.market_book_add(symbol)
            print("Market-depth subscription successful:", subscribed)

            if subscribed:
                book = mt5.market_book_get(symbol)

                if book is None:
                    print("Market-depth data unavailable:", mt5.last_error())
                else:
                    print("Market-depth entries returned:", len(book))

                    type_names = {
                        mt5.BOOK_TYPE_SELL: "SELL",
                        mt5.BOOK_TYPE_BUY: "BUY",
                        mt5.BOOK_TYPE_SELL_MARKET: "SELL_MARKET",
                        mt5.BOOK_TYPE_BUY_MARKET: "BUY_MARKET",
                    }

                    for level in list(book)[:10]:
                        side = type_names.get(level.type, str(level.type))
                        volume = getattr(level, "volume_dbl", level.volume)
                        print(
                            "Level:",
                            side,
                            "Price:", level.price,
                            "Volume:", volume
                        )

                mt5.market_book_release(symbol)
            else:
                print("Market depth could not be subscribed.")

    finally:
        mt5.shutdown()
        print("\nMT5 connection closed by diagnostic script.")


if __name__ == "__main__":
    main()
