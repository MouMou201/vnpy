"""Backtest a daily CTA strategy with vnpy_ctastrategy.

Loads daily K-line bars from a local CSV (default) or directly from OSS, feeds
them into vnpy_ctastrategy's BacktestingEngine, and prints performance stats.

Data source:
- default: local CSV (data/example_daily_candlestick.csv)
- --from-oss with --symbol: read data/a_stock/<symbol>.csv from OSS

Examples:
    python scripts/run_backtest.py
    python scripts/run_backtest.py --symbol 000006.SZSE --from-oss
    python scripts/run_backtest.py --start 2018-01-01 --capital 1000000 --chart
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData
from vnpy.trader.utility import extract_vt_symbol
from vnpy_ctastrategy.backtesting import BacktestingEngine

# Make repo root and scripts dir importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from strategies.boll_channel_daily_strategy import BollChannelDailyStrategy  # noqa: E402

DEFAULT_LOCAL_CSV = REPO_ROOT / "data" / "example_daily_candlestick.csv"
DEFAULT_BUCKET = "oss-pai-031vpz46w8hpgwnvap-cn-shanghai"
DEFAULT_ENDPOINT = "https://oss-cn-shanghai.aliyuncs.com"


def bars_from_df(df: pd.DataFrame) -> list[BarData]:
    """Convert a candlestick dataframe into daily BarData objects."""
    if df.empty:
        return []

    df = df.copy()
    df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    bars: list[BarData] = []
    for row in df.to_dict("records"):
        symbol, exchange = extract_vt_symbol(str(row["symbol"]))
        bars.append(
            BarData(
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
                gateway_name="BACKTEST",
            )
        )
    return bars


def load_df_from_oss(symbol: str, remote_prefix: str, bucket_name: str, endpoint: str) -> pd.DataFrame | None:
    """Download a per-stock CSV from OSS into a dataframe."""
    try:
        import oss2
    except ImportError:
        print("Missing dependency 'oss2'. Install it first:\n    pip install oss2")
        return None

    from upload_to_oss import load_credentials

    key_id, key_secret = load_credentials()
    if not key_id or not key_secret:
        print("Missing OSS credentials (env vars or data/oss_credentials.local.json).")
        return None

    auth = oss2.Auth(key_id, key_secret)
    bucket = oss2.Bucket(auth, endpoint, bucket_name)
    key = f"{remote_prefix.strip('/')}/{symbol}.csv"
    try:
        raw = bucket.get_object(key).read()
    except oss2.exceptions.OssError as exc:
        print(f"Failed to read {key} from OSS: {exc}")
        return None
    return pd.read_csv(io.BytesIO(raw))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest a daily CTA strategy.")
    parser.add_argument("--symbol", default="300285.SZSE", help="vt_symbol, e.g. 300285.SZSE.")
    parser.add_argument("--from-oss", action="store_true", help="Load data from OSS instead of local CSV.")
    parser.add_argument("--input", default=None, help="Local CSV path (when not --from-oss).")
    parser.add_argument("--remote-prefix", default="data/a_stock", help="OSS key prefix.")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="OSS bucket name.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="OSS endpoint URL.")

    parser.add_argument("--start", default=None, help="Backtest start date YYYY-MM-DD.")
    parser.add_argument("--end", default=None, help="Backtest end date YYYY-MM-DD.")
    parser.add_argument("--capital", type=float, default=1_000_000, help="Initial capital.")
    parser.add_argument("--rate", type=float, default=0.0003, help="Commission rate.")
    parser.add_argument("--slippage", type=float, default=0.01, help="Slippage per trade price.")
    parser.add_argument("--size", type=float, default=1, help="Contract multiplier (stocks=1).")
    parser.add_argument("--pricetick", type=float, default=0.01, help="Minimum price tick.")

    # Strategy parameters
    parser.add_argument("--boll-window", type=int, default=20)
    parser.add_argument("--boll-dev", type=float, default=2.0)
    parser.add_argument("--atr-window", type=int, default=20)
    parser.add_argument("--sl-multiplier", type=float, default=3.0)
    parser.add_argument("--trend-window", type=int, default=60)
    parser.add_argument("--fixed-size", type=int, default=100, help="Shares per trade.")
    parser.add_argument("--allow-short", type=int, default=0, choices=[0, 1])

    parser.add_argument(
        "--no-chart",
        action="store_true",
        help="Skip the interactive K-line window (results are still saved to data/backtest/).",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "data" / "backtest"),
        help="Directory to save graphical results.",
    )
    args = parser.parse_args()

    # Load data
    if args.from_oss:
        df = load_df_from_oss(args.symbol, args.remote_prefix, args.bucket, args.endpoint)
    else:
        csv_path = Path(args.input).resolve() if args.input else DEFAULT_LOCAL_CSV
        if not csv_path.exists():
            print(f"Input CSV not found: {csv_path}")
            return
        df = pd.read_csv(csv_path)

    if df is None or df.empty:
        print("No data loaded.")
        return

    bars = bars_from_df(df)
    if not bars:
        print("No bars after parsing.")
        return

    # Determine date range from data if not given.
    data_start = bars[0].datetime
    data_end = bars[-1].datetime
    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else data_start
    end = datetime.strptime(args.end, "%Y-%m-%d") if args.end else data_end

    bars = [b for b in bars if start <= b.datetime <= end]
    if len(bars) < args.trend_window + 10:
        print(f"Not enough bars ({len(bars)}) for trend_window={args.trend_window}.")
        return

    # Configure the engine.
    engine = BacktestingEngine()
    engine.set_parameters(
        vt_symbol=args.symbol,
        interval=Interval.DAILY,
        start=start,
        end=end,
        rate=args.rate,
        slippage=args.slippage,
        size=args.size,
        pricetick=args.pricetick,
        capital=args.capital,
    )
    engine.add_strategy(
        BollChannelDailyStrategy,
        {
            "boll_window": args.boll_window,
            "boll_dev": args.boll_dev,
            "atr_window": args.atr_window,
            "sl_multiplier": args.sl_multiplier,
            "trend_window": args.trend_window,
            "fixed_size": args.fixed_size,
            "allow_short": args.allow_short,
        },
    )

    # Inject bars directly (no database needed).
    engine.history_data = bars

    print(
        f"Backtesting {args.symbol} | {start.date()} -> {end.date()} | "
        f"{len(bars)} daily bars"
    )
    engine.run_backtesting()
    engine.calculate_result()
    statistics = engine.calculate_statistics(output=True)

    trades = list(engine.trades.values())

    # Always save graphical results to the output directory.
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    kline_path = out_dir / f"{args.symbol}_kline.html"
    save_kline_html(bars, trades, statistics, args.symbol, kline_path)
    print(f"Saved K-line chart to: {kline_path}")

    perf_fig = engine.show_chart()
    if perf_fig is not None:
        # Match the K-line chart style/interaction: dark theme, drag-to-pan,
        # wheel-zoom (scrollZoom).
        perf_fig.update_layout(template="plotly_dark", dragmode="pan")
        perf_path = out_dir / f"{args.symbol}_performance.html"
        perf_fig.write_html(str(perf_path), config={"scrollZoom": True})
        print(f"Saved performance report to: {perf_path}")

    # Default: also show the interactive vnpy.chart K-line window with trade markers.
    if not args.no_chart:
        show_result_chart(
            bars=bars,
            trades=trades,
            statistics=statistics,
            title=f"{args.symbol} backtest",
        )


def save_kline_html(
    bars: list[BarData],
    trades: list,
    statistics: dict,
    symbol: str,
    out_path: Path,
) -> None:
    """Save a dark-themed, zoomable candlestick + volume + trade-marker HTML."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    from vnpy.trader.constant import Direction

    dates = [b.datetime for b in bars]
    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        row_heights=[0.74, 0.26],
        vertical_spacing=0.03,
        subplot_titles=("Price", "Volume"),
    )

    fig.add_trace(
        go.Candlestick(
            x=dates,
            open=[b.open_price for b in bars],
            high=[b.high_price for b in bars],
            low=[b.low_price for b in bars],
            close=[b.close_price for b in bars],
            name="K-line",
            increasing_line_color="#ef5350",   # CN style: up = red
            decreasing_line_color="#26a69a",   # down = green
        ),
        row=1,
        col=1,
    )

    buy_x = [t.datetime for t in trades if t.direction == Direction.LONG]
    buy_y = [t.price for t in trades if t.direction == Direction.LONG]
    sell_x = [t.datetime for t in trades if t.direction != Direction.LONG]
    sell_y = [t.price for t in trades if t.direction != Direction.LONG]

    if buy_x:
        fig.add_trace(
            go.Scatter(
                x=buy_x, y=buy_y, mode="markers", name="buy / cover",
                marker={"symbol": "triangle-up", "size": 10, "color": "#00e676"},
            ),
            row=1, col=1,
        )
    if sell_x:
        fig.add_trace(
            go.Scatter(
                x=sell_x, y=sell_y, mode="markers", name="sell / short",
                marker={"symbol": "triangle-down", "size": 10, "color": "#ff1744"},
            ),
            row=1, col=1,
        )

    fig.add_trace(
        go.Bar(x=dates, y=[b.volume for b in bars], name="Volume", marker_color="#5c6bc0"),
        row=2, col=1,
    )

    def pct(key: str) -> str:
        v = statistics.get(key)
        return f"{v:.2f}%" if isinstance(v, (int, float)) else "N/A"

    def num(key: str) -> str:
        v = statistics.get(key)
        return format(v, ",.2f") if isinstance(v, (int, float)) else "N/A"

    title = (
        f"{symbol} backtest  |  Return {pct('total_return')}  "
        f"Annual {pct('annual_return')}  Sharpe {num('sharpe_ratio')}  "
        f"MaxDD {pct('max_ddpercent')}  Trades {statistics.get('total_trade_count', 'N/A')}"
    )

    fig.update_layout(
        template="plotly_dark",
        title=title,
        xaxis_rangeslider_visible=False,
        height=900,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
        hovermode="x unified",
        dragmode="pan",   # drag to pan, like vnpy.chart
    )
    # scrollZoom: wheel to zoom, matching the vnpy.chart interaction feel.
    fig.write_html(str(out_path), config={"scrollZoom": True})


def show_result_chart(
    bars: list[BarData],
    trades: list,
    statistics: dict,
    title: str,
) -> None:
    """Show an interactive K-line chart (black, smooth) with buy/sell markers."""
    import pyqtgraph as pg

    from vnpy.trader.constant import Direction
    from vnpy.trader.ui import create_qapp
    from vnpy.chart import ChartWidget, CandleItem, VolumeItem

    app = create_qapp()

    widget = ChartWidget()
    widget.add_plot("candle", hide_x_axis=True)
    widget.add_plot("volume", maximum_height=160)
    widget.add_item(CandleItem, "candle", "candle")
    widget.add_item(VolumeItem, "volume", "volume")
    widget.add_cursor()
    widget.update_history(bars)

    # Map datetime -> bar index for placing trade markers.
    dt_index = {bar.datetime: ix for ix, bar in enumerate(bars)}

    buy_spots: list = []
    sell_spots: list = []
    for trade in trades:
        ix = dt_index.get(trade.datetime)
        if ix is None:
            continue
        spot = {"pos": (ix, trade.price), "size": 14}
        if trade.direction == Direction.LONG:
            spot.update({"symbol": "t1", "brush": pg.mkBrush(0, 220, 0), "pen": pg.mkPen("w", width=0.5)})
            buy_spots.append(spot)
        else:
            spot.update({"symbol": "t", "brush": pg.mkBrush(220, 0, 0), "pen": pg.mkPen("w", width=0.5)})
            sell_spots.append(spot)

    candle_plot = widget.get_plot("candle")
    if buy_spots or sell_spots:
        scatter = pg.ScatterPlotItem()
        scatter.addPoints(buy_spots + sell_spots)
        scatter.setZValue(2)
        candle_plot.addItem(scatter)

    # On-chart stats panel (top-left of the candle plot).
    def pct(key: str) -> str:
        v = statistics.get(key)
        return f"{v:.2f}%" if isinstance(v, (int, float)) else "N/A"

    def num(key: str, fmt: str = ",.2f") -> str:
        v = statistics.get(key)
        return format(v, fmt) if isinstance(v, (int, float)) else "N/A"

    summary = (
        f"{title}\n"
        f"Total Return: {pct('total_return')}   "
        f"Annual: {pct('annual_return')}\n"
        f"Sharpe: {num('sharpe_ratio')}   "
        f"Max DD: {pct('max_ddpercent')}\n"
        f"End Balance: {num('end_balance')}   "
        f"Trades: {statistics.get('total_trade_count', 'N/A')}\n"
        f"green ▲ buy / cover    red ▼ sell / short"
    )
    text_item = pg.TextItem(summary, color="w", anchor=(0, 0), fill=pg.mkBrush(0, 0, 0, 160))
    text_item.setZValue(3)
    candle_plot.addItem(text_item)

    def place_text() -> None:
        view_range = candle_plot.getViewBox().viewRange()
        text_item.setPos(view_range[0][0], view_range[1][1])

    place_text()
    candle_plot.getViewBox().sigRangeChanged.connect(lambda *_: place_text())

    widget.setWindowTitle(
        f"{title}  |  Return {pct('total_return')}  Sharpe {num('sharpe_ratio')}  "
        f"MaxDD {pct('max_ddpercent')}"
    )
    widget.showMaximized()
    app.exec()


if __name__ == "__main__":
    main()
