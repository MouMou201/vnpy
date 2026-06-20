"""Bollinger Channel breakout strategy (daily bars).

A trend-following CTA strategy adapted for daily stock/futures K-line:

- Entry: close breaks above the upper Bollinger band while above a long-term
  trend filter (SMA) -> go long. (Optionally the mirror condition -> go short.)
- Exit: ATR-based trailing stop, or close falling back below the middle/lower
  band.

Design adapted from the classic VeighNa community "Boll Channel" strategy
(https://github.com/vnpy/vnpy_ctastrategy, strategies/boll_channel_strategy.py),
which uses Bollinger bands for entry and an ATR trailing stop for exit. The
original works on intraday 15-minute bars; this version operates directly on
daily bars and adds a long-term SMA trend filter, which suits daily A-share data.
"""

from vnpy_ctastrategy import (
    CtaTemplate,
    StopOrder,
    TickData,
    BarData,
    TradeData,
    OrderData,
    BarGenerator,
    ArrayManager,
)


class BollChannelDailyStrategy(CtaTemplate):
    """Bollinger channel breakout with trend filter and ATR trailing stop."""

    author = "adapted from VeighNa community Boll Channel strategy"

    # Parameters
    boll_window: int = 20
    boll_dev: float = 2.0
    atr_window: int = 20
    sl_multiplier: float = 3.0
    trend_window: int = 60
    fixed_size: int = 100
    allow_short: int = 0          # 0 = long-only (stocks), 1 = allow short (futures)

    # Variables
    boll_up: float = 0.0
    boll_down: float = 0.0
    boll_mid: float = 0.0
    atr_value: float = 0.0
    trend_ma: float = 0.0
    intra_trade_high: float = 0.0
    intra_trade_low: float = 0.0
    long_stop: float = 0.0
    short_stop: float = 0.0

    parameters = [
        "boll_window",
        "boll_dev",
        "atr_window",
        "sl_multiplier",
        "trend_window",
        "fixed_size",
        "allow_short",
    ]
    variables = [
        "boll_up",
        "boll_down",
        "boll_mid",
        "atr_value",
        "trend_ma",
        "long_stop",
        "short_stop",
    ]

    def on_init(self) -> None:
        """Callback when strategy is inited."""
        self.write_log("策略初始化")
        self.bg = BarGenerator(self.on_bar)
        # ArrayManager must hold enough history for the longest window.
        size = max(self.boll_window, self.atr_window, self.trend_window) + 10
        self.am = ArrayManager(size=size)
        self.load_bar(self.trend_window + 10)

    def on_start(self) -> None:
        """Callback when strategy is started."""
        self.write_log("策略启动")
        self.put_event()

    def on_stop(self) -> None:
        """Callback when strategy is stopped."""
        self.write_log("策略停止")

    def on_tick(self, tick: TickData) -> None:
        """Callback of new tick data update."""
        self.bg.update_tick(tick)

    def on_bar(self, bar: BarData) -> None:
        """Core logic: runs on each (daily) bar."""
        self.cancel_all()

        am = self.am
        am.update_bar(bar)
        if not am.inited:
            return

        self.boll_up, self.boll_down = am.boll(self.boll_window, self.boll_dev)
        self.boll_mid = am.sma(self.boll_window)
        self.atr_value = am.atr(self.atr_window)
        self.trend_ma = am.sma(self.trend_window)

        if self.pos == 0:
            self.intra_trade_high = bar.high_price
            self.intra_trade_low = bar.low_price

            # Long when price breaks the upper band and is in an uptrend.
            if bar.close_price > self.boll_up and bar.close_price > self.trend_ma:
                self.buy(bar.close_price, self.fixed_size)
            # Mirror short entry, only if shorting is allowed.
            elif (
                self.allow_short
                and bar.close_price < self.boll_down
                and bar.close_price < self.trend_ma
            ):
                self.short(bar.close_price, self.fixed_size)

        elif self.pos > 0:
            self.intra_trade_high = max(self.intra_trade_high, bar.high_price)
            self.long_stop = self.intra_trade_high - self.atr_value * self.sl_multiplier

            # Exit on ATR trailing stop or loss of the band midline.
            if bar.close_price < self.long_stop or bar.close_price < self.boll_mid:
                self.sell(bar.close_price, abs(self.pos))

        elif self.pos < 0:
            self.intra_trade_low = min(self.intra_trade_low, bar.low_price)
            self.short_stop = self.intra_trade_low + self.atr_value * self.sl_multiplier

            if bar.close_price > self.short_stop or bar.close_price > self.boll_mid:
                self.cover(bar.close_price, abs(self.pos))

        self.put_event()

    def on_order(self, order: OrderData) -> None:
        """Callback of new order data update."""
        pass

    def on_trade(self, trade: TradeData) -> None:
        """Callback of new trade data update."""
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """Callback of stop order update."""
        pass
