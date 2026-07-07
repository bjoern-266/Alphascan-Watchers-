"""
AlphaScan-Watcher
Scannt eine Watchlist, prueft AlphaScan-Kriterien und sendet Push nur
bei bestandenem Setup. Laeuft nur im Zeitfenster 15:30-22:30 Europe/Berlin.
Schreibt zusaetzlich results.json / history.json fuer das Web-Dashboard.
"""

import os
import sys
import json
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf
import pandas as pd

WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMD", "IBM", "MU",
    "DTG.DE", "CENT", "SLB", "PYPL",
]

WINDOW_START = dtime(15, 30)
WINDOW_END = dtime(22, 30)

MIN_CRV = 2.0
MIN_DAILY_TURNOVER_USD = 1_000_000
RSI_MIN, RSI_MAX = 40, 70

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "bjoern-alphascan-privat")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"

LOOKBACK_DAYS = "6mo"
RESULTS_FILE = "results.json"
HISTORY_FILE = "history.json"
MAX_HISTORY = 100


def within_trading_window() -> bool:
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    if now.weekday() >= 5:
        print(f"[{now}] Wochenende - kein Scan.")
        return False
    current_time = now.time()
    if not (WINDOW_START <= current_time <= WINDOW_END):
        print(f"[{now}] Ausserhalb des Zeitfensters - kein Scan.")
        return False
    return True


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def find_support_resistance(data: pd.DataFrame, window: int = 20):
    recent = data.tail(window)
    support = recent["Low"].min()
    resistance = recent["High"].max()
    return float(support), float(resistance)


def check_ticker(ticker: str):
    try:
        data = yf.download(ticker, period=LOOKBACK_DAYS, interval="1d", progress=False)
    except Exception as e:
        print(f"  Fehler beim Laden von {ticker}: {e}")
        return {"ticker": ticker, "error": str(e)}

    if data.empty or len(data) < 200:
        print(f"  {ticker}: zu wenig Daten - uebersprungen.")
        return {"ticker": ticker, "error": "zu wenig Kursdaten"}

    data["MA50"] = data["Close"].rolling(50).mean()
    data["MA200"] = data["Close"].rolling(200).mean()
    data["RSI"] = compute_rsi(data["Close"])

    last = data.iloc[-1]
    close = float(last["Close"])
    ma50 = float(last["MA50"])
    ma200 = float(last["MA200"])
    rsi = float(last["RSI"])
    avg_volume = float(data["Volume"].tail(30).mean())
    daily_turnover = avg_volume * close

    strict_trend_ok = close > ma50 and close > ma200
    rsi_ok = RSI_MIN < rsi < RSI_MAX
    liquidity_ok = daily_turnover >= MIN_DAILY_TURNOVER_USD

    support, resistance = find_support_resistance(data)
    stop = support * 0.98
    target = resistance
    risk = close - stop
    reward = target - close
    crv = reward / risk if risk > 0 and reward > 0 else 0
    crv_ok = crv >= MIN_CRV

    pct_change_5d = (close / float(data["Close"].iloc[-6]) - 1) * 100 if len(data) > 6 else 0
    no_extreme_jump = pct_change_5d < 15

    checks = {
        "Trend (ueber MA50 & MA200)": strict_trend_ok,
        f"RSI im Korridor ({RSI_MIN}-{RSI_MAX})": rsi_ok,
        f"Liquiditaet (>{MIN_DAILY_TURNOVER_USD:,.0f}$/Tag)": liquidity_ok,
        f"CRV >= {MIN_CRV}": crv_ok,
        "Kein Kurssprung >15% in 5 Tagen": no_extreme_jump,
    }
    passed = all(checks.values())

    return {
        "ticker": ticker,
        "close": round(close, 2),
        "rsi": round(rsi, 1),
        "stop": round(stop, 2),
        "target": round(target, 2),
        "crv": round(crv, 2),
        "daily_turnover": round(daily_turnover, 0),
        "checks": checks,
        "passed": passed,
    }


def send_push(setup):
    message = (
        f"{setup['ticker']}: moegliches Setup erkannt\n"
        f"Kurs: {setup['close']:.2f}\n"
        f"Stop-Loss: {setup['stop']:.2f}\n"
        f"Kursziel: {setup['target']:.2f}\n"
        f"CRV: {setup['crv']:.1f}:1 | RSI: {setup['rsi']:.0f}\n"
        f"Automatischer Vorcheck bestanden - vor Kauf manuell News/Earnings pruefen!"
    )
    try:
        resp = requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={"Title": f"AlphaScan: {setup['ticker']}", "Priority": "default"},
            timeout=10,
        )
        print(f"  Push gesendet fuer {setup['ticker']} (Status {resp.status_code})")
    except Exception as e:
        print(f"  Fehler beim Senden: {e}")


def write_dashboard_files(scan_timestamp: str, results: list, skipped: bool, skip_reason: str = ""):
    payload = {
        "timestamp": scan_timestamp,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "results": results,
    }
    with open(RESULTS_FILE, "w") as f:
        json.dump(payload, f, indent=2)

    history = []
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                history = json.load(f)
        except Exception:
            history = []

    history.append(payload)
    history = history[-MAX_HISTORY:]

    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)


def main():
    now_str = datetime.now(ZoneInfo("Europe/Berlin")).isoformat()

    if not within_trading_window():
        write_dashboard_files(now_str, [], skipped=True, skip_reason="Ausserhalb Zeitfenster oder Wochenende")
        sys.exit(0)

    print(f"Scan gestartet: {now_str}")
    results = []
    any_hit = False

    for ticker in WATCHLIST:
        print(f"Pruefe {ticker} ...")
        result = check_ticker(ticker)
        results.append(result)

        if result.get("error"):
            continue

        for check_name, ok in result["checks"].items():
            print(f"    - {check_name}: {'OK' if ok else 'NICHT erfuellt'}")

        if result["passed"]:
            print(f"  --> {ticker}: ALLE Kriterien erfuellt, sende Push.")
            send_push(result)
            any_hit = True
        else:
            print(f"  --> {ticker}: kein vollstaendiges Setup, kein Push.")

    write_dashboard_files(now_str, results, skipped=False)

    if not any_hit:
        print("Kein Ticker hat alle Kriterien erfuellt - keine Nachricht.")


if __name__ == "__main__":
    main()
