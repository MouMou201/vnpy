"""Plot a daily K-line (candlestick) chart from a CSV using vnpy.chart.

Reads a CSV produced by ``get_daily_candlestick.py`` (columns: date, symbol,
open, high, low, close, volume, amount, turnover_rate) and shows an
interactive candlestick + volume window built on vnpy's pyqtgraph chart.

Usage:
    python scripts/plot_daily_candlestick.py
    python scripts/plot_daily_candlestick.py --input data/example_daily_candlestick.csv
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from vnpy.trader.ui import create_qapp
from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData
from vnpy.trader.utility import extract_vt_symbol
from vnpy.chart import ChartWidget, CandleItem, VolumeItem


def load_bars_from_csv(csv_path: Path) -> list[BarData]:
    """Load a CSV file into a list of daily BarData objects."""
    df: pd.DataFrame = pd.read_csv(csv_path)
    if df.empty:
        return []

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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot daily candlestick chart from CSV using vnpy.chart."
    )
    parser.add_argument(
        "--input",
        type=str,
        default=str(
            Path(__file__).resolve().parents[1] / "data" / "example_daily_candlestick.csv"
        ),
        help="Input CSV path.",
    )
    args = parser.parse_args()

    csv_path = Path(args.input).resolve()
    if not csv_path.exists():
        print(f"Input CSV not found: {csv_path}")
        return

    bars = load_bars_from_csv(csv_path)
    if not bars:
        print(f"No bars loaded from: {csv_path}")
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
