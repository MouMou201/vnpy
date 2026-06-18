"""Fetch full daily K-line (candlestick) history for one A-share stock.

Downloads from the stock's listing date to today and saves to a CSV.
Example stock: 300285 (国瓷材料).

Primary source is Sina (``stock_zh_a_daily``), which is stable and push2-free;
EastMoney (``stock_zh_a_hist``) is used as a fallback.

Output columns (one trading day per row):
- date
- symbol
- open / high / low / close
- volume
- amount
- turnover_rate
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import akshare as ak
import pandas as pd

OUTPUT_COLUMNS = [
    "date",
    "symbol",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "turnover_rate",
]


def exp_backoff_sleep(
    attempt: int,
    base_delay: float,
    backoff_factor: float,
    max_delay: float,
) -> None:
    """Sleep with exponential backoff by attempt number."""
    wait_seconds: float = min(base_delay * (backoff_factor ** (attempt - 1)), max_delay)
    time.sleep(wait_seconds)


def infer_exchange(code: str) -> str:
    """Infer exchange suffix from a 6-digit stock code."""
    if code.startswith(("600", "601", "603", "605", "688", "689", "900")):
        return "SSE"
    if code.startswith(("000", "001", "002", "003", "300", "301", "200")):
        return "SZSE"
    if code.startswith(("4", "8", "92")):
        return "BSE"
    return "UNKNOWN"


def sina_symbol(code: str) -> str:
    """Build the Sina-style prefixed symbol (e.g. sz300285)."""
    prefix = {"SSE": "sh", "SZSE": "sz", "BSE": "bj"}.get(infer_exchange(code), "")
    return f"{prefix}{code}"


def fetch_from_sina(code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """Fetch daily history from Sina and normalize to OUTPUT_COLUMNS."""
    df = ak.stock_zh_a_daily(
        symbol=sina_symbol(code),
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    if df is None or df.empty:
        raise RuntimeError(f"Sina returned no data for {code}.")

    # Sina columns: date, open, high, low, close, volume, amount,
    # outstanding_share, turnover (turnover ratio).
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["date"], errors="coerce")
    out["open"] = pd.to_numeric(df["open"], errors="coerce")
    out["high"] = pd.to_numeric(df["high"], errors="coerce")
    out["low"] = pd.to_numeric(df["low"], errors="coerce")
    out["close"] = pd.to_numeric(df["close"], errors="coerce")
    out["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    out["amount"] = pd.to_numeric(df.get("amount"), errors="coerce")
    out["turnover_rate"] = pd.to_numeric(df.get("turnover"), errors="coerce")
    return out


def fetch_from_eastmoney(code: str, start_date: str, end_date: str, adjust: str) -> pd.DataFrame:
    """Fetch daily history from EastMoney and normalize to OUTPUT_COLUMNS."""
    df = ak.stock_zh_a_hist(
        symbol=code,
        period="daily",
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
    )
    if df is None or df.empty:
        raise RuntimeError(f"EastMoney returned no data for {code}.")

    # EastMoney columns: 日期,股票代码,开盘,收盘,最高,最低,成交量,成交额,
    # 振幅,涨跌幅,涨跌额,换手率
    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df["日期"], errors="coerce")
    out["open"] = pd.to_numeric(df["开盘"], errors="coerce")
    out["high"] = pd.to_numeric(df["最高"], errors="coerce")
    out["low"] = pd.to_numeric(df["最低"], errors="coerce")
    out["close"] = pd.to_numeric(df["收盘"], errors="coerce")
    out["volume"] = pd.to_numeric(df["成交量"], errors="coerce")
    out["amount"] = pd.to_numeric(df["成交额"], errors="coerce")
    out["turnover_rate"] = pd.to_numeric(df["换手率"], errors="coerce")
    return out


def fetch_daily_history(
    code: str,
    start_date: str,
    end_date: str,
    adjust: str,
    retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> pd.DataFrame:
    """Fetch daily K-line, trying Sina then EastMoney, with retry/backoff."""
    sources = [("Sina", fetch_from_sina), ("EastMoney", fetch_from_eastmoney)]
    last_error: Exception | None = None

    for source_name, source_func in sources:
        for attempt in range(1, retries + 1):
            try:
                return source_func(code, start_date, end_date, adjust)
            except Exception as exc:  # pragma: no cover - network dependent
                last_error = exc
                if attempt == retries:
                    print(f"{source_name} failed after {retries} attempts: {exc}")
                    break
                print(
                    f"{source_name} fetch failed ({attempt}/{retries}): {exc}. "
                    "Retrying with exponential backoff..."
                )
                exp_backoff_sleep(
                    attempt=attempt,
                    base_delay=delay_seconds,
                    backoff_factor=backoff_factor,
                    max_delay=max_delay,
                )

    raise RuntimeError(
        f"Failed to fetch daily history for {code} from all sources."
    ) from last_error


def build_daily_candlestick(
    code: str,
    start_date: str,
    end_date: str,
    adjust: str,
    retries: int,
    delay_seconds: float,
    backoff_factor: float,
    max_delay: float,
) -> pd.DataFrame:
    """Build a normalized daily candlestick dataframe for one stock."""
    df = fetch_daily_history(
        code=code,
        start_date=start_date,
        end_date=end_date,
        adjust=adjust,
        retries=retries,
        delay_seconds=delay_seconds,
        backoff_factor=backoff_factor,
        max_delay=max_delay,
    )

    df["symbol"] = f"{code}.{infer_exchange(code)}"
    df = df.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")

    return df[OUTPUT_COLUMNS]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch full daily K-line history for one A-share stock since listing."
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="300285",
        help="6-digit stock code (e.g. 300285).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(
            Path(__file__).resolve().parents[1] / "data" / "example_daily_candlestick.csv"
        ),
        help="Output CSV path.",
    )
    parser.add_argument(
        "--adjust",
        type=str,
        default="qfq",
        choices=["", "qfq", "hfq"],
        help="Price adjustment: '' raw, 'qfq' forward-adjusted, 'hfq' backward-adjusted.",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        default="19900101",
        help="Start date YYYYMMDD; defaults early to cover the listing date.",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=time.strftime("%Y%m%d"),
        help="End date YYYYMMDD; defaults to today.",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Retry count per source.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.8,
        help="Base retry delay seconds.",
    )
    parser.add_argument(
        "--backoff-factor",
        type=float,
        default=2.0,
        help="Exponential backoff factor.",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=8.0,
        help="Maximum backoff delay seconds.",
    )
    args = parser.parse_args()

    code: str = str(args.symbol).zfill(6)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        df = build_daily_candlestick(
            code=code,
            start_date=args.start_date,
            end_date=args.end_date,
            adjust=args.adjust,
            retries=args.retries,
            delay_seconds=args.delay,
            backoff_factor=args.backoff_factor,
            max_delay=args.max_delay,
        )
    except KeyboardInterrupt:
        print("\nInterrupted by user before any data was saved.")
        return
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"Failed to fetch daily candlestick for {code}.\nReason: {exc}")
        return

    df.to_csv(output_path, index=False, encoding="utf-8-sig")
    first_date = df["date"].iloc[0] if len(df) else "N/A"
    last_date = df["date"].iloc[-1] if len(df) else "N/A"
    print(
        f"Saved {len(df)} daily bars for {code} "
        f"({first_date} -> {last_date}) to: {output_path}"
    )


if __name__ == "__main__":
    main()
