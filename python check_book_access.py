
import MetaTrader5 as mt5


def main():
    if not mt5.initialize():
        print("MT5 initialization failed:", mt5.last_error())
        return

    try:
        terminal = mt5.terminal_info()
        account = mt5.account_info()

        print("TERMINAL PATH:", terminal.path if terminal else None)
        print("TERMINAL CONNECTED:", terminal.connected if terminal else None)
        print("ACCOUNT SERVER:", account.server if account else None)

        symbol = "XAUUSDm"
        info = mt5.symbol_info(symbol)

        print("\nSYMBOL:", symbol)
        print("SYMBOL FOUND:", info is not None)

        if info is None:
            return

        if not info.visible:
            print("SYMBOL SELECTED:", mt5.symbol_select(symbol, True))
        else:
            print("SYMBOL SELECTED: True")

        print("CURRENT QUOTE:", mt5.symbol_info_tick(symbol))

        subscribed = mt5.market_book_add(symbol)
        print("\nBOOK SUBSCRIPTION SUCCESSFUL:", subscribed)
        print("ERROR DETAILS:", mt5.last_error())

        if subscribed:
            book = mt5.market_book_get(symbol)
            print("BOOK IS NONE:", book is None)

            if book is not None:
                print("BOOK ENTRIES:", len(book))

                for level in list(book)[:10]:
                    print(level._asdict())

            mt5.market_book_release(symbol)
        else:
            print("Python could not subscribe to the order book.")

    finally:
        mt5.shutdown()
        print("\nDiagnostic finished. No trades were placed.")


if __name__ == "__main__":
    main()
