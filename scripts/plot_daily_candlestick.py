"""Plot a daily K-line (candlestick) chart using vnpy.chart.

Data sources:
- ``--symbol 000006.SZSE``: read the per-stock CSV directly from OSS
  (``data/a_stock/<symbol>.csv``) and plot it.
- ``--input <path>``: read a local CSV file.
- neither: default to the local ``data/example_daily_candlestick.csv``.

Expected CSV columns: date, symbol, open, high, low, close, volume,
amount[, turnover_rate].

Usage:
    python scripts/plot_daily_candlestick.py                  # default local example
    python scripts/plot_daily_candlestick.py --symbol 000006.SZSE
    python scripts/plot_daily_candlestick.py --input data/example_daily_candlestick.csv
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import pandas as pd

from vnpy.trader.ui import create_qapp
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData
from vnpy.trader.utility import extract_vt_symbol
from vnpy.chart import ChartWidget, CandleItem, VolumeItem

# Make sibling scripts importable when run as `python scripts/plot_daily_candlestick.py`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_LOCAL_CSV = (
    Path(__file__).resolve().parents[1] / "data" / "example_daily_candlestick.csv"
)


def load_bars_from_df(df: pd.DataFrame) -> list[BarData]:
    """Convert a candlestick dataframe into a list of daily BarData objects."""
    if df.empty:
        return []

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    bars: list[BarData] = []
    for row in df.to_dict("records"):
        symbol, exchange = extract_vt_symbol(str(row["symbol"]))
        bar = BarData(
            symbol=symbol,
            exchange=exchange,
            datetime=row["datetime"].to_pydatetime(),
            interval=Interval.DAILY,
            open_price=float(row["open"]),
            high_price=float(row["high"]),
            low_price=float(row["low"]),
            close_price=float(row["close"]),
            volume=float(row.get("volume", 0) or 0),
            turnover=float(row.get("amount", 0) or 0),
            open_interest=0,
            gateway_name="CSV",
        )
        bars.append(bar)

    return bars


def load_df_from_oss(
    symbol: str, remote_prefix: str, bucket_name: str, endpoint: str, is_cname: bool
) -> pd.DataFrame | None:
    """Download a per-stock CSV from OSS into a dataframe."""
    try:
        import oss2
    except ImportError:
        print("Missing dependency 'oss2'. Install it first:\n    pip install oss2")
        return None

    from upload_to_oss import load_credentials

    key_id, key_secret = load_credentials()
    if not key_id or not key_secret:
        print(
            "Missing credentials. Set OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET "
            "or create data/oss_credentials.local.json."
        )
        return None

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name, is_cname=is_cname)
    key = f"{remote_prefix.strip('/')}/{symbol}.csv"

    try:
        raw = bucket.get_object(key).read()
    except oss2.exceptions.NoSuchKey:
        print(f"Object not found on OSS: {key}")
        return None
    except oss2.exceptions.OssError as exc:
        print(f"Failed to read {key} from OSS: {exc}")
        return None

    return pd.read_csv(io.BytesIO(raw))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot daily candlestick chart from OSS or a local CSV using vnpy.chart."
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Stock symbol (e.g. 000006.SZSE) to read from OSS data/a_stock/.",
    )
    parser.add_argument(
        "--input",
        type=str,
        default=None,
        help="Local CSV path (used when --symbol is not given).",
    )
    parser.add_argument(
        "--remote-prefix",
        default="data/a_stock",
        help="Remote key prefix where per-stock CSVs live.",
    )
    parser.add_argument(
        "--bucket",
        default="oss-pai-031vpz46w8hpgwnvap-cn-shanghai",
        help="OSS bucket name.",
    )
    parser.add_argument(
        "--endpoint",
        default="https://oss-cn-shanghai.aliyuncs.com",
        help="OSS endpoint URL.",
    )
    parser.add_argument("--cname", action="store_true", help="Treat endpoint as a CNAME domain.")
    args = parser.parse_args()

    if args.symbol:
        df = load_df_from_oss(
            symbol=args.symbol,
            remote_prefix=args.remote_prefix,
            bucket_name=args.bucket,
            endpoint=args.endpoint,
            is_cname=args.cname,
        )
        if df is None:
            return
        source_label = f"oss://{args.bucket}/{args.remote_prefix}/{args.symbol}.csv"
    else:
        csv_path = Path(args.input).resolve() if args.input else DEFAULT_LOCAL_CSV
        if not csv_path.exists():
            print(f"Input CSV not found: {csv_path}")
            return
        df = pd.read_csv(csv_path)
        source_label = str(csv_path)

    bars = load_bars_from_df(df)
    if not bars:
        print(f"No bars loaded from: {source_label}")
        return

    app = create_qapp()

    widget = ChartWidget()
    widget.add_plot("candle", hide_x_axis=True)
    widget.add_plot("volume", maximum_height=200)
    widget.add_item(CandleItem, "candle", "candle")
    widget.add_item(VolumeItem, "volume", "volume")
    widget.add_cursor()

    widget.update_history(bars)
    widget.setWindowTitle(f"{bars[0].vt_symbol} daily K-line ({len(bars)} bars)")
    widget.show()

    app.exec()


if __name__ == "__main__":
    main()
