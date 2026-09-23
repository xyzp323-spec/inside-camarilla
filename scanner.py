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

IST = ZoneInfo("Asia/Kolkata")

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
}


# ============================================================
# NSE SESSION
# ============================================================

def get_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


# ============================================================
# DOWNLOAD NSE ZIP
# ============================================================

def download_zip(session, url):

    response = session.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    if response.content[:2] != b"PK":
        raise RuntimeError(
            f"NSE did not return a ZIP file. "
            f"HTTP {response.status_code}"
        )

    return zipfile.ZipFile(
        io.BytesIO(response.content)
    )


# ============================================================
# FIND LATEST TRADING DAY
# ============================================================

def get_latest_trading_date():

    today = datetime.now(IST).date()

    for days_back in range(0, 10):

        date = today - timedelta(
            days=days_back
        )

        # Skip Saturday and Sunday
        if date.weekday() >= 5:
            continue

        url = (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_"
            f"{date.strftime('%Y%m%d')}_F_0000.csv.zip"
        )

        try:

            response = requests.get(
                url,
                headers=HEADERS,
                timeout=20
            )

            if (
                response.status_code == 200
                and response.content[:2] == b"PK"
            ):
                return date, response.content

        except Exception:
            pass

    raise RuntimeError(
        "Could not find the latest NSE CM UDiFF bhavcopy."
    )


# ============================================================
# READ NSE BHAVCOPY
# ============================================================

def read_cm_bhavcopy(zip_bytes):

    zip_file = zipfile.ZipFile(
        io.BytesIO(zip_bytes)
    )

    csv_files = [
        name
        for name in zip_file.namelist()
        if name.lower().endswith(".csv")
    ]

    if not csv_files:
        raise RuntimeError(
            "No CSV found inside NSE CM bhavcopy ZIP."
        )

    with zip_file.open(csv_files[0]) as file:

        df = pd.read_csv(file)

    # NSE UDiFF column names are uppercase
    df.columns = [
        str(column).strip().upper()
        for column in df.columns
    ]

    return df


# ============================================================
# GET CURRENT F&O UNIVERSE
# ============================================================

def get_fno_universe(session):

    print("Downloading current NSE F&O universe...")

    url = (
        "https://nsearchives.nseindia.com/content/fo/"
        "fo_mktlots.csv"
    )

    response = session.get(
        url,
        timeout=30
    )

    response.raise_for_status()

    text = response.content.decode(
        "utf-8-sig",
        errors="replace"
    )

    df = pd.read_csv(
        io.StringIO(text),
        header=None
    )

    stocks = set()

    for column in df.columns:

        for value in df[column].astype(str):

            symbol = value.strip().upper()

            if not symbol:
                continue

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

            if (
                1 <= len(symbol) <= 30
                and " " not in symbol
                and "," not in symbol
                and "/" not in symbol
                and not symbol.isdigit()
            ):
                stocks.add(symbol)

    print(
        "Symbols found in F&O source:",
        len(stocks)
    )

    return stocks


# ============================================================
# FIND COLUMN
# ============================================================

def find_column(df, possible_names):

    for name in possible_names:

        if name in df.columns:
            return name

    return None


# ============================================================
# PREPARE EQUITY DATA
# ============================================================

def prepare_equity_data(df):

    # Current NSE UDiFF names
    symbol_col = find_column(
        df,
        [
            "TCKRSYMB",
            "TckrSymb",
            "SYMBOL",
            "Symbol"
        ]
    )

    open_col = find_column(
        df,
        [
            "OPNPRIC",
            "OpnPric",
            "OPEN",
            "OPEN_PRICE"
        ]
    )

    high_col = find_column(
        df,
        [
            "HGHPRIC",
            "HghPric",
            "HIGH",
            "HIGH_PRICE"
        ]
    )

    low_col = find_column(
        df,
        [
            "LWPRIC",
            "LwPric",
            "LOW",
            "LOW_PRICE"
        ]
    )

    close_col = find_column(
        df,
        [
            "CLSPRIC",
            "ClsPric",
            "CLOSE",
            "CLOSE_PRICE"
        ]
    )

    series_col = find_column(
        df,
        [
            "SCTYSRS",
            "SctySrs",
            "SERIES"
        ]
    )

    if not all([
        symbol_col,
        open_col,
        high_col,
        low_col,
        close_col
    ]):

        raise RuntimeError(
            "Could not identify OHLC columns in "
            "NSE UDiFF file.\n"
            f"Columns found: {list(df.columns)}"
        )

    output = pd.DataFrame()

    output["SYMBOL"] = (
        df[symbol_col]
        .astype(str)
        .str.strip()
        .str.upper()
    )

    output["OPEN"] = pd.to_numeric(
        df[open_col],
        errors="coerce"
    )

    output["HIGH"] = pd.to_numeric(
        df[high_col],
        errors="coerce"
    )

    output["LOW"] = pd.to_numeric(
        df[low_col],
        errors="coerce"
    )

    output["CLOSE"] = pd.to_numeric(
        df[close_col],
        errors="coerce"
    )

    if series_col:

        output["SERIES"] = (
            df[series_col]
            .astype(str)
            .str.strip()
            .str.upper()
        )

    else:

        output["SERIES"] = ""

    output = output.dropna(
        subset=[
            "OPEN",
            "HIGH",
            "LOW",
            "CLOSE"
        ]
    )

    # Keep normal equity series
    output = output[
        (output["SERIES"] == "") |
        (output["SERIES"] == "EQ")
    ]

    return output


# ============================================================
# CAMARILLA
# ============================================================

def camarilla(high, low, close):

    h4 = close + (
        (high - low) * 1.1 / 2
    )

    l4 = close - (
        (high - low) * 1.1 / 2
    )

    return h4, l4


# ============================================================
# SCAN INSIDE CAM
# ============================================================

def scan_inside_cam(
    today_df,
    yesterday_df,
    fno_symbols
):

    today = today_df[
        today_df["SYMBOL"].isin(fno_symbols)
    ].copy()

    yesterday = yesterday_df[
        yesterday_df["SYMBOL"].isin(fno_symbols)
    ].copy()

    today = today.set_index("SYMBOL")
    yesterday = yesterday.set_index("SYMBOL")

    common_symbols = sorted(
        set(today.index) &
        set(yesterday.index)
    )

    print(
        "F&O stocks with data for both days:",
        len(common_symbols)
    )

    results = []

    for symbol in common_symbols:

        today_row = today.loc[symbol]
        yesterday_row = yesterday.loc[symbol]

        today_h4, today_l4 = camarilla(
            today_row["HIGH"],
            today_row["LOW"],
            today_row["CLOSE"]
        )

        yesterday_h4, yesterday_l4 = camarilla(
            yesterday_row["HIGH"],
            yesterday_row["LOW"],
            yesterday_row["CLOSE"]
        )

        # EXACT INSIDE CAM CONDITION
        #
        # Today's H4 <= Yesterday's H4
        # Today's L4 >= Yesterday's L4

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
                "yesterday_l4": yesterday_l4
            })

    return results


# ============================================================
# SEND TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=30
    )

    response.raise_for_status()


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "Starting Inside Camarilla scanner..."
    )

    session = get_session()

    # --------------------------------------------------------
    # 1. FIND LATEST TRADING DAY
    # --------------------------------------------------------

    latest_date, latest_zip = (
        get_latest_trading_date()
    )

    print(
        "Latest NSE trading date:",
        latest_date
    )

    # --------------------------------------------------------
    # 2. READ TODAY'S EQUITY DATA
    # --------------------------------------------------------

    today_df_raw = read_cm_bhavcopy(
        latest_zip
    )

    today_df = prepare_equity_data(
        today_df_raw
    )

    print(
        "Today's equity records:",
        len(today_df)
    )

    # --------------------------------------------------------
    # 3. FIND PREVIOUS TRADING DAY
    # --------------------------------------------------------

    previous_zip = None
    previous_date = None

    for days_back in range(1, 10):

        date = latest_date - timedelta(
            days=days_back
        )

        if date.weekday() >= 5:
            continue

        url = (
            "https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_"
            f"{date.strftime('%Y%m%d')}_F_0000.csv.zip"
        )

        try:

            response = session.get(
                url,
                timeout=30
            )

            if (
                response.status_code == 200
                and response.content[:2] == b"PK"
            ):

                previous_zip = response.content
                previous_date = date

                break

        except Exception:
            pass

    if previous_zip is None:

        raise RuntimeError(
            "Could not find previous NSE trading "
            "day's bhavcopy."
        )

    print(
        "Previous NSE trading date:",
        previous_date
    )

    # --------------------------------------------------------
    # 4. READ PREVIOUS DAY
    # --------------------------------------------------------

    yesterday_df_raw = read_cm_bhavcopy(
        previous_zip
    )

    yesterday_df = prepare_equity_data(
        yesterday_df_raw
    )

    print(
        "Previous day equity records:",
        len(yesterday_df)
    )

    # --------------------------------------------------------
    # 5. CURRENT F&O UNIVERSE
    # --------------------------------------------------------

    fno_symbols = get_fno_universe(
        session
    )

    # --------------------------------------------------------
    # 6. SCAN
    # --------------------------------------------------------

    results = scan_inside_cam(
        today_df,
        yesterday_df,
        fno_symbols
    )

    # --------------------------------------------------------
    # 7. TELEGRAM MESSAGE
    # --------------------------------------------------------

    date_text = latest_date.strftime(
        "%d-%b-%Y"
    )

    message = (
        "INSIDE CAMARILLA\n"
        f"{date_text}\n\n"
    )

    if results:

        for item in results:

            message += (
                f"{item['symbol']}\n"
                f"H4: {item['today_h4']:.2f} | "
                f"L4: {item['today_l4']:.2f}\n\n"
            )

        message += (
            f"Total: {len(results)}\n"
            f"F&O universe checked: "
            f"{len(fno_symbols)}"
        )

    else:

        message += (
            "No stocks found.\n\n"
            f"F&O universe checked: "
            f"{len(fno_symbols)}"
        )

    # --------------------------------------------------------
    # 8. SEND TELEGRAM
    # --------------------------------------------------------

    send_telegram(message)

    print(
        "Scanner completed successfully."
    )

    print(
        "Inside Cam stocks:",
        len(results)
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()
