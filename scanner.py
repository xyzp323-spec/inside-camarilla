import os
import io
import zipfile
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

IST = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


def get_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def download_zip(session, url):
    r = session.get(url, timeout=30)
    r.raise_for_status()

    if not r.content[:2] == b"PK":
        raise RuntimeError(
            f"NSE did not return a ZIP file. HTTP {r.status_code}"
        )

    return zipfile.ZipFile(io.BytesIO(r.content))


def get_latest_trading_date():
    """
    Try today and the previous few calendar days.
    This automatically handles weekends and NSE holidays.
    """
    today = datetime.now(IST).date()

    for days_back in range(0, 10):
        d = today - timedelta(days=days_back)

        # Saturday / Sunday
        if d.weekday() >= 5:
            continue

        # We use the existence of the CM UDiFF file
        # to determine whether NSE had a trading report.
        url = (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
        )

        try:
            r = requests.get(url, headers=HEADERS, timeout=20)

            if r.status_code == 200 and r.content[:2] == b"PK":
                return d, r.content

        except Exception:
            pass

    raise RuntimeError("Could not find the latest NSE CM UDiFF bhavcopy.")


def read_cm_bhavcopy(zip_bytes):
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))

    csv_files = [
        x for x in z.namelist()
        if x.lower().endswith(".csv")
    ]

    if not csv_files:
        raise RuntimeError("No CSV found inside NSE CM bhavcopy ZIP.")

    with z.open(csv_files[0]) as f:
        df = pd.read_csv(f)

    df.columns = [str(c).strip().upper() for c in df.columns]

    return df


def get_fno_universe(session):
    """
    Download NSE's current F&O market-lot file.

    This is refreshed every time the scanner runs.
    Therefore the stock universe is NOT permanently hard-coded.
    """

    url = (
        "https://nsearchives.nseindia.com/content/fo/"
        "fo_mktlots.csv"
    )

    r = session.get(url, timeout=30)
    r.raise_for_status()

    text = r.content.decode("utf-8-sig", errors="replace")

    # Read the CSV flexibly.
    df = pd.read_csv(io.StringIO(text), header=None)

    stocks = set()

    for col in df.columns:
        for value in df[col].astype(str):
            symbol = value.strip().upper()

            if not symbol:
                continue

            # Ignore obvious headers and non-symbol text.
            if symbol in {
                "SYMBOL",
                "SYMBOLS",
                "INDEX",
                "UNDERLYING",
                "NIFTY",
                "BANKNIFTY",
                "FINNIFTY",
                "MIDCPNIFTY",
                "NIFTYNXT50",
            }:
                continue

            # Normal NSE symbols are generally short.
            if (
                1 <= len(symbol) <= 30
                and " " not in symbol
                and "," not in symbol
                and "/" not in symbol
            ):
                # Avoid purely numeric values.
                if not symbol.isdigit():
                    stocks.add(symbol)

    # The lot file may contain other fields.
    # Keep only symbols that actually appear in the
    # equity bhavcopy later.
    return stocks


def find_column(df, possible_names):
    for name in possible_names:
        if name in df.columns:
            return name

    return None


def prepare_equity_data(df):
    symbol_col = find_column(
        df,
        ["TckrSymb", "SYMBOL", "Symbol"]
    )

    open_col = find_column(
        df,
        ["OpnPric", "OPEN", "OPEN_PRICE"]
    )

    high_col = find_column(
        df,
        ["HghPric", "HIGH", "HIGH_PRICE"]
    )

    low_col = find_column(
        df,
        ["LwPric", "LOW", "LOW_PRICE"]
    )

    close_col = find_column(
        df,
        ["ClsPric", "CLOSE", "CLOSE_PRICE"]
    )

    series_col = find_column(
        df,
        ["SctySrs", "SERIES"]
    )

    if not all(
        [symbol_col, open_col, high_col, low_col, close_col]
    ):
        raise RuntimeError(
            "Could not identify OHLC columns in NSE UDiFF file.\n"
            f"Columns found: {list(df.columns)}"
        )

    out = pd.DataFrame()

    out["SYMBOL"] = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    out["OPEN"] = pd.to_numeric(
        df[open_col], errors="coerce"
    )

    out["HIGH"] = pd.to_numeric(
        df[high_col], errors="coerce"
    )

    out["LOW"] = pd.to_numeric(
        df[low_col], errors="coerce"
    )

    out["CLOSE"] = pd.to_numeric(
        df[close_col], errors="coerce"
    )

    if series_col:
        out["SERIES"] = (
            df[series_col]
            .astype(str)
            .str.strip()
            .str.upper()
        )
    else:
        out["SERIES"] = ""

    out = out.dropna(
        subset=["OPEN", "HIGH", "LOW", "CLOSE"]
    )

    # Equity normal series.
    out = out[
        (out["SERIES"] == "") |
        (out["SERIES"] == "EQ")
    ]

    return out


def camarilla(high, low, close):
    h4 = close + ((high - low) * 1.1 / 2)
    l4 = close - ((high - low) * 1.1 / 2)

    return h4, l4


def scan_inside_cam(today_df, yesterday_df, fno_symbols):

    today = today_df[
        today_df["SYMBOL"].isin(fno_symbols)
    ].copy()

    yesterday = yesterday_df[
        yesterday_df["SYMBOL"].isin(fno_symbols)
    ].copy()

    today = today.set_index("SYMBOL")
    yesterday = yesterday.set_index("SYMBOL")

    common = sorted(
        set(today.index) & set(yesterday.index)
    )

    results = []

    for symbol in common:

        t = today.loc[symbol]
        y = yesterday.loc[symbol]

        today_h4, today_l4 = camarilla(
            t["HIGH"],
            t["LOW"],
            t["CLOSE"]
        )

        yesterday_h4, yesterday_l4 = camarilla(
            y["HIGH"],
            y["LOW"],
            y["CLOSE"]
        )

        # YOUR EXACT INSIDE CAM CONDITION
        if (
            today_h4 <= yesterday_h4
            and
            today_l4 >= yesterday_l4
        ):
            results.append({
                "symbol": symbol,
                "today_h4": today_h4,
                "today_l4": today_l4,
                "yesterday_h4": yesterday_h4,
                "yesterday_l4": yesterday_l4,
            })

    return results


def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
        },
        timeout=30,
    )

    response.raise_for_status()


def main():

    print("Starting Inside Camarilla scanner...")

    session = get_session()

    # --------------------------------------------------------
    # 1. Find latest NSE trading day
    # --------------------------------------------------------

    latest_date, latest_zip = get_latest_trading_date()

    print(
        "Latest NSE trading date:",
        latest_date
    )

    # --------------------------------------------------------
    # 2. Download today's equity bhavcopy
    # --------------------------------------------------------

    today_df_raw = read_cm_bhavcopy(latest_zip)

    today_df = prepare_equity_data(today_df_raw)

    # --------------------------------------------------------
    # 3. Download previous trading day's bhavcopy
    # --------------------------------------------------------

    previous_zip = None

    for days_back in range(1, 10):

        d = latest_date - timedelta(days=days_back)

        if d.weekday() >= 5:
            continue

        url = (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip"
        )

        try:
            r = session.get(
                url,
                timeout=30
            )

            if (
                r.status_code == 200
                and r.content[:2] == b"PK"
            ):
                previous_zip = r.content
                previous_date = d
                break

        except Exception:
            pass

    if previous_zip is None:
        raise RuntimeError(
            "Could not find previous NSE trading day's bhavcopy."
        )

    yesterday_df_raw = read_cm_bhavcopy(
        previous_zip
    )

    yesterday_df = prepare_equity_data(
        yesterday_df_raw
    )

    # --------------------------------------------------------
    # 4. Get CURRENT F&O universe
    # --------------------------------------------------------

    fno_symbols = get_fno_universe(session)

    print(
        "Symbols in current F&O source:",
        len(fno_symbols)
    )

    # --------------------------------------------------------
    # 5. Scan
    # --------------------------------------------------------

    results = scan_inside_cam(
        today_df,
        yesterday_df,
        fno_symbols
    )

    # --------------------------------------------------------
    # 6. Telegram message
    # --------------------------------------------------------

    date_text = latest_date.strftime("%d-%b-%Y")

    message = (
        "INSIDE CAMARILLA\n"
        f"{date_text}\n\n"
    )

    if results:

        for item in results:

            message += (
                f"{item['symbol']}\n"
                f"H4: {item['today_h4']:.2f} | "
                f"L4: {item['today_l4']:.2f}\n"
            )

            message += "\n"

        message += (
            f"Total: {len(results)}\n"
            f"F&O universe checked: {len(fno_symbols)}"
        )

    else:

        message += (
            "No stocks found.\n\n"
            f"F&O universe checked: {len(fno_symbols)}"
        )

    send_telegram(message)

    print("Scanner completed successfully.")
    print(
        "Inside Cam stocks:",
        len(results)
    )


if __name__ == "__main__":
    main()
