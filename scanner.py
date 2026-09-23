import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# ============================================================
# SETTINGS
# ============================================================

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

NSE_HOME = "https://www.nseindia.com"
FNO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"

IST = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Connection": "keep-alive",
}


# ============================================================
# NSE SESSION
# ============================================================

def create_nse_session():
    session = requests.Session()
    session.headers.update(HEADERS)

    r = session.get(NSE_HOME, timeout=20)
    r.raise_for_status()

    return session


# ============================================================
# CURRENT F&O UNIVERSE
# ============================================================

def get_current_fno_stocks(session):
    """
    NSE's current permitted-lot-size file contains eligible
    equity-derivative scrips and indices.

    We use it every time the scanner runs, so the F&O universe
    automatically follows NSE changes.
    """

    r = session.get(FNO_LOTS_URL, timeout=20)
    r.raise_for_status()

    text = r.content.decode("utf-8-sig", errors="replace")

    rows = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            continue

        parts = [x.strip() for x in line.split(",")]

        if len(parts) < 3:
            continue

        symbol = parts[1].strip().upper()

        # Skip header
        if symbol in ("SYMBOL", "SYMBOLS", "CODE"):
            continue

        # We only want individual stock underlyings.
        # Index symbols are excluded.
        index_symbols = {
            "NIFTY",
            "BANKNIFTY",
            "FINNIFTY",
            "MIDCPNIFTY",
            "NIFTYNXT50",
        }

        if symbol in index_symbols:
            continue

        # Keep normal NSE symbols.
        if symbol and symbol.replace("&", "").replace("-", "").replace("_", "").isalnum():
            rows.append(symbol)

    stocks = sorted(set(rows))

    if len(stocks) < 50:
        raise RuntimeError(
            f"NSE F&O universe download looks abnormal. "
            f"Only {len(stocks)} symbols found."
        )

    return stocks


# ============================================================
# NSE EQUITY DAILY DATA
# ============================================================

def get_equity_history(session, symbol):
    """
    NSE historical equity API.
    Gets enough recent daily data to obtain the latest two
    completed trading sessions.
    """

    end_date = datetime.now(IST).date()
    start_date = end_date - timedelta(days=12)

    url = (
        "https://www.nseindia.com/api/historical/cm/equity"
        f"?symbol={symbol}"
        f"&from={start_date.strftime('%d-%m-%Y')}"
        f"&to={end_date.strftime('%d-%m-%Y')}"
    )

    r = session.get(url, timeout=20)

    if r.status_code != 200:
        return None

    data = r.json()

    records = data.get("data", [])

    if not records:
        return None

    rows = []

    for item in records:
        try:
            rows.append(
                {
                    "date": pd.to_datetime(item["CH_TIMESTAMP"]),
                    "open": float(item["CH_OPENING_PRICE"]),
                    "high": float(item["CH_TRADE_HIGH_PRICE"]),
                    "low": float(item["CH_TRADE_LOW_PRICE"]),
                    "close": float(item["CH_CLOSING_PRICE"]),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    if len(rows) < 2:
        return None

    df = pd.DataFrame(rows)
    df = df.sort_values("date").reset_index(drop=True)

    return df


# ============================================================
# CAMARILLA
# ============================================================

def camarilla_h4(high, low, close):
    return close + ((high - low) * 1.1 / 2)


def camarilla_l4(high, low, close):
    return close - ((high - low) * 1.1 / 2)


# ============================================================
# INSIDE CAM
# ============================================================

def is_inside_cam(df):
    """
    User's exact definition:

    Today's H4 <= Yesterday's H4
    AND
    Today's L4 >= Yesterday's L4
    """

    yesterday = df.iloc[-2]
    today = df.iloc[-1]

    yesterday_h4 = camarilla_h4(
        yesterday["high"],
        yesterday["low"],
        yesterday["close"],
    )

    yesterday_l4 = camarilla_l4(
        yesterday["high"],
        yesterday["low"],
        yesterday["close"],
    )

    today_h4 = camarilla_h4(
        today["high"],
        today["low"],
        today["close"],
    )

    today_l4 = camarilla_l4(
        today["high"],
        today["low"],
        today["close"],
    )

    inside = (
        today_h4 <= yesterday_h4
        and
        today_l4 >= yesterday_l4
    )

    return inside, {
        "date": today["date"],
        "today_h4": today_h4,
        "today_l4": today_l4,
        "yesterday_h4": yesterday_h4,
        "yesterday_l4": yesterday_l4,
    }


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    r = requests.post(url, data=payload, timeout=20)
    r.raise_for_status()


# ============================================================
# MAIN SCANNER
# ============================================================

def main():

    session = create_nse_session()

    print("Getting current NSE F&O universe...")

    stocks = get_current_fno_stocks(session)

    print(f"Current F&O stocks found: {len(stocks)}")

    inside_cam = []

    failed = 0

    for i, symbol in enumerate(stocks, start=1):

        print(f"{i}/{len(stocks)}  {symbol}")

        try:
            df = get_equity_history(session, symbol)

            if df is None:
                failed += 1
                continue

            result, levels = is_inside_cam(df)

            if result:
                inside_cam.append(
                    (
                        symbol,
                        levels["today_h4"],
                        levels["today_l4"],
                        levels["yesterday_h4"],
                        levels["yesterday_l4"],
                    )
                )

        except Exception as e:
            print(f"Error with {symbol}: {e}")
            failed += 1

    today = datetime.now(IST).strftime("%d-%b-%Y")

    # ========================================================
    # TELEGRAM MESSAGE
    # ========================================================

    if inside_cam:

        message = f"INSIDE CAMARILLA\n{today}\n\n"

        for item in inside_cam:
            symbol = item[0]
            today_h4 = item[1]
            today_l4 = item[2]

            message += (
                f"{symbol}\n"
                f"H4: {today_h4:.2f} | "
                f"L4: {today_l4:.2f}\n\n"
            )

        message += (
            f"Total: {len(inside_cam)}\n"
            f"F&O universe scanned: {len(stocks)}"
        )

    else:

        message = (
            f"INSIDE CAMARILLA\n"
            f"{today}\n\n"
            f"No stocks found.\n\n"
            f"F&O universe scanned: {len(stocks)}"
        )

    send_telegram(message)

    print("\nDone.")
    print(f"Inside Cam stocks: {len(inside_cam)}")
    print(f"Failed/Unavailable: {failed}")


if __name__ == "__main__":
    main()
