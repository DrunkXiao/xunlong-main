"""中间区域：单股精盯视图与多股对比网格。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QButtonGroup, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from app.db.models import Bar, Quote
from app.services.intraday import SamplePoint
from app.ui.charts import (
    CANDLE_SCHEMES, CANDLE_SCHEME_TIP, CandleChart, IntradayChart,
    IntradayMoneyFlowChart, MiniChart, MoneyFlowChart, is_minute_source,
)
from app.ui.panels import _label
from app.ui.tdx_link_button import TdxLinkButton
from app.ui.theme import COLORS, color_for_change, format_amount, format_pct

PERIODS = [("intraday", "分时"), ("1d", "日K"), ("1w", "周K"), ("1M", "月K")]
GRID_SIZES = [2, 4, 6, 9, 12]


class InfoBar(QWidget):
    """单股顶部信息条：名称/代码/最新价/涨跌/成交额/成交量/换手/量比。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(14)

        self.name = QLabel("未选择股票")
        self.name.setFont(QFont("Microsoft YaHei UI", 13, QFont.DemiBold))
        self.code = _label("--", "muted")
        self.price = QLabel("--")
        self.price.setFont(QFont("Consolas", 20, QFont.DemiBold))
        self.change = QLabel("--")
        self.change.setFont(QFont("Consolas", 11))
        self.fields: dict[str, QLabel] = {}
        for key, text in (("amount", "成交额 --"), ("volume", "成交量 --"),
                          ("turnover", "换手 --"), ("ratio", "量比 --"),
                          ("limits", "涨停/跌停 --")):
            label = _label(text, "muted")
            self.fields[key] = label

        layout.addWidget(self.name)
        layout.addWidget(self.code)
        layout.addWidget(self.price)
        layout.addWidget(self.change)
        for key in ("amount", "volume", "turnover", "ratio", "limits"):
            layout.addWidget(self.fields[key])
        layout.addStretch(1)
        self.badge = _label("", "muted")
        layout.addWidget(self.badge)

    def set_quote(self, quote: Quote) -> None:
        self.name.setText(quote.name or quote.symbol)
        self.code.setText(quote.symbol)
        color = color_for_change(quote.change_pct)
        self.price.setText(f"{quote.price:.2f}" if quote.price is not None else "--")
        self.price.setStyleSheet(f"color:{color};")
        change_amount = (quote.price - quote.prev_close) if (quote.price and quote.prev_close) else None
        self.change.setText(
            f"{change_amount:+.2f} {format_pct(quote.change_pct)}" if change_amount is not None
            else format_pct(quote.change_pct)
        )
        self.change.setStyleSheet(f"color:{color};")
        hands = f"{quote.volume / 100:.0f} 手" if quote.volume else "--"
        self.fields["amount"].setText(f"成交额 {format_amount(quote.amount)}")
        self.fields["volume"].setText(f"成交量 {hands}")
        turnover = f"{quote.turnover_rate:.2f}%" if quote.turnover_rate is not None else "--"
        self.fields["turnover"].setText(f"换手 {turnover}")
        self.fields["ratio"].setText(
            f"量比 {quote.volume_ratio:.2f}" if quote.volume_ratio is not None else "量比 --")
        limits = "--"
        if quote.limit_up_price or quote.limit_down_price:
            up = f"{quote.limit_up_price:.2f}" if quote.limit_up_price else "--"
            down = f"{quote.limit_down_price:.2f}" if quote.limit_down_price else "--"
            limits = f"涨停 {up} / 跌停 {down}"
        self.fields["limits"].setText(limits)
        self.badge.setText(f"{quote.data_level} 级 · {quote.source or '未知源'}")

    def clear(self) -> None:
        self.name.setText("未选择股票")
        self.code.setText("--")
        self.price.setText("--")
        self.change.setText("--")
        for key in self.fields:
            self.fields[key].setText("--")


class SingleStockView(QWidget):
    period_changed = Signal(str)
    scheme_changed = Signal(str)        # K 线配色方案：change / ma
    time_clicked = Signal(object)      # 分时图点击 -> datetime
    tdx_message = Signal(str)          # 通达信联动的结果文案（写日志/状态栏用）

    def __init__(self, config: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.info = InfoBar()
        layout.addWidget(self.info)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.period_group = QButtonGroup(self)
        self.period_group.setExclusive(True)
        self.period_buttons: dict[str, QPushButton] = {}
        for key, text in PERIODS:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setFixedWidth(56)
            btn.clicked.connect(lambda _=False, k=key: self.period_changed.emit(k))
            self.period_group.addButton(btn)
            self.period_buttons[key] = btn
            bar.addWidget(btn)
        self.period_buttons["1d"].setChecked(True)

        # K 线配色方案：切换即写设置，重启后保持（铁律 7）
        self.scheme = QComboBox()
        self.scheme.setToolTip(CANDLE_SCHEME_TIP)
        for key, text in CANDLE_SCHEMES.items():
            self.scheme.addItem(text, key)
        self.scheme.setFixedWidth(96)
        self.scheme.currentIndexChanged.connect(self._on_scheme_changed)
        bar.addSpacing(6)
        bar.addWidget(self.scheme)

        # 通达信联动：紧跟在「涨跌配色」后面，同样 8px 等距、同宽 96，样式沿用全局
        # QPushButton（与周期按钮同源，高度自然一致）；没选股票时置灰。
        self.btn_tdx = TdxLinkButton(config)
        self.btn_tdx.setFixedWidth(96)
        self.btn_tdx.message.connect(self.tdx_message)
        bar.addSpacing(6)
        bar.addWidget(self.btn_tdx)

        bar.addStretch(1)
        self.status = _label("", "muted")
        bar.addWidget(self.status)
        layout.addLayout(bar)

        self.stack = QStackedWidget()
        self.candles = CandleChart()
        self.intraday = IntradayChart()
        self.intraday.point_clicked.connect(self.time_clicked)

        # 日K 区 = 主图 + 资金流副图（主力资金：当日净额柱 + 累计主力/超大单净流入）。
        # 副图绑主图，缩放/拖动逐根对齐。
        # 周K/月K 下方不显示：那张表按交易日给数，聚合成周/月要另定口径
        # （周净额是求和还是均值、跨周缺日怎么算），没有明确口径前不画。
        self.candle_box = QWidget()
        candle_layout = QVBoxLayout(self.candle_box)
        candle_layout.setContentsMargins(0, 0, 0, 0)
        candle_layout.setSpacing(2)
        candle_layout.addWidget(self.candles, 1)
        self.money_flow = MoneyFlowChart()
        self.money_flow.bind(self.candles)
        candle_layout.addWidget(self.money_flow)

        # 分时区 = 分时图 + 资金流副图（分钟级累计净额，东财 fflow）。
        # 两张图共用 241 槽时间轴与左右边距，09:30 / 11:30 / 15:00 落在同一条竖线上；
        # 副图固定高度，窗口变矮时优先压分时图，不把五条线挤成一条。
        self.intraday_box = QWidget()
        intraday_layout = QVBoxLayout(self.intraday_box)
        intraday_layout.setContentsMargins(0, 0, 0, 0)
        intraday_layout.setSpacing(2)
        intraday_layout.addWidget(self.intraday, 1)
        self.intraday_flow = IntradayMoneyFlowChart()
        intraday_layout.addWidget(self.intraday_flow)

        self.stack.addWidget(self.candle_box)
        self.stack.addWidget(self.intraday_box)
        layout.addWidget(self.stack, 1)

        self._period = "1d"
        self._symbol = ""

    def set_period(self, period: str) -> None:
        if period not in self.period_buttons:
            period = "1d"
        self._period = period
        self.period_buttons[period].setChecked(True)
        self.stack.setCurrentWidget(self.intraday_box if period == "intraday"
                                    else self.candle_box)
        self.money_flow.setVisible(period == "1d")

    # ------------------------------------------------------------ 配色方案
    def _on_scheme_changed(self, *_args) -> None:
        scheme = self.scheme.currentData() or "change"
        self.candles.set_candle_scheme(scheme)
        self.scheme_changed.emit(scheme)

    def set_candle_scheme(self, scheme: str) -> None:
        """恢复设置时调用：只更新控件与图，不再回发信号，避免启动时反复写库。"""
        if scheme not in CANDLE_SCHEMES:
            scheme = "change"
        index = self.scheme.findData(scheme)
        if index >= 0:
            self.scheme.blockSignals(True)
            self.scheme.setCurrentIndex(index)
            self.scheme.blockSignals(False)
        self.candles.set_candle_scheme(scheme)

    def current_candle_scheme(self) -> str:
        return self.scheme.currentData() or "change"

    def current_period(self) -> str:
        return self._period

    def set_symbol(self, symbol: str) -> None:
        self._symbol = symbol
        self.btn_tdx.set_symbol(symbol)
        self.status.setText("加载中…")

    def clear_symbol(self) -> None:
        """没有有效标的（空分组）时调用：联动按钮跟着置灰，避免把旧代码发出去。"""
        self._symbol = ""
        self.btn_tdx.set_symbol(None)

    def set_quote(self, quote: Quote) -> None:
        self.info.set_quote(quote)
        self.status.setText("")

    def set_bars(self, bars: list[Bar], label: str, level: str, quality: str = "exchange") -> None:
        self.candles.set_bars(bars, label, level, quality)
        self.stack.setCurrentWidget(self.candle_box)
        self.status.setText(f"{len(bars)} 根" if bars else "无K线返回")

    def set_money_flow(self, rows, source_label: str = "", data_level: str = "") -> None:
        """填充「主力资金」副图。`rows` 是带 `.key`/`.main_net`/`.jumbo_net` 的资金流记录。

        这里转成 `{"YYYY-MM-DD": 净额}` 再交给图：图表只认日期→数值，
        不关心数据来自哪个适配器，换源时不用改绘制代码。主力净额与超大单净额
        分两个字典传，副图才能画出两条独立的累计线。
        """
        flow: dict[str, float] = {}
        jumbo: dict[str, float] = {}
        for row in rows or []:
            key = getattr(row, "key", "")
            if not key:
                continue
            value = getattr(row, "main_net", None)
            if value is not None:
                flow[key] = float(value)
            extra = getattr(row, "jumbo_net", None)
            if extra is not None:
                jumbo[key] = float(extra)
        self.money_flow.set_flow(flow, source_label, data_level, jumbo=jumbo)

    def clear_money_flow(self, reason: str = "无数据：资金流尚未返回") -> None:
        self.money_flow.clear_flow(reason)

    def set_intraday(self, points: list[SamplePoint], prev_close: float | None,
                     limit_up: float | None, limit_down: float | None,
                     title: str, level: str, source: str = "sampled") -> None:
        self.intraday.set_series(points, prev_close, limit_up, limit_down, title, level,
                                 source=source)
        self.stack.setCurrentWidget(self.intraday_box)
        if not points:
            self.status.setText("等待分钟线返回" if is_minute_source(source) else "等待首个采样点")
        elif is_minute_source(source):
            self.status.setText(f"分钟点 {len(points)}/241" if len(points) <= 241
                                else f"分钟点 {len(points)}")
        else:
            self.status.setText(f"采样点 {len(points)}")

    # ------------------------------------------------------------ 分时资金流
    def set_intraday_money_flow(self, points, source_label: str = "",
                                data_level: str = "") -> None:
        """填充分时页下方「资金流」副图。

        `points` 是东财分钟资金流点（已带 `.time` 与五档净额字段）；图只认这些字段，
        不关心数据来自哪个适配器，换源时不用改绘制代码。
        """
        self.intraday_flow.set_points(points, source_label, data_level)

    def clear_intraday_money_flow(self, reason: str = "无数据：资金流尚未返回") -> None:
        self.intraday_flow.clear_points(reason)

    # ------------------------------------------------------------ 联动
    def set_intraday_markers(self, orders: list) -> None:
        self.intraday.set_markers(orders)

    def set_intraday_focus(self, moment) -> None:
        self.intraday.set_focus(moment)

    def set_event_marks(self, marks: dict) -> None:
        self.candles.set_event_marks(marks)

    def set_focus_date(self, day) -> None:
        self.candles.set_focus_date(day)

    def set_lhb_marks(self, marks: dict) -> None:
        """日K 上的龙虎榜标记（披露事实，区别于区间大单三角）。"""
        self.candles.set_lhb_marks(marks)

    def show_error(self, text: str) -> None:
        self.candles.empty_reason = text
        self.intraday.empty_reason = text
        self.money_flow.clear_flow(text)
        self.intraday_flow.clear_points(text)
        self.status.setText(text)
        self.candles.update()
        self.intraday.update()


class GridCell(QWidget):
    activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.symbol = ""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 2, 3, 2)
        layout.setSpacing(1)
        header = QHBoxLayout()
        header.setSpacing(6)
        self.title = QLabel("空")
        self.title.setFont(QFont("Microsoft YaHei UI", 9, QFont.DemiBold))
        self.price = QLabel("--")
        self.price.setFont(QFont("Consolas", 10, QFont.DemiBold))
        self.pct = QLabel("--")
        self.pct.setFont(QFont("Consolas", 9))
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.price)
        header.addWidget(self.pct)
        layout.addLayout(header)
        self.chart = MiniChart()
        layout.addWidget(self.chart, 1)
        self.setMinimumSize(180, 130)

    def set_symbol(self, symbol: str, name: str) -> None:
        self.symbol = symbol
        self.title.setText(f"{name} {symbol}" if symbol else "空")
        self.price.setText("--")
        self.pct.setText("--")
        self.chart.points = []
        self.chart.title = ""
        self.chart.update()

    def update_quote(self, quote: Quote) -> None:
        color = color_for_change(quote.change_pct)
        self.price.setText(f"{quote.price:.2f}" if quote.price is not None else "--")
        self.price.setStyleSheet(f"color:{color};")
        self.pct.setText(format_pct(quote.change_pct))
        self.pct.setStyleSheet(f"color:{color};")
        if quote.name:
            self.title.setText(f"{quote.name} {quote.symbol}")

    def update_series(self, points: list[SamplePoint], prev_close: float | None,
                      level: str, source: str = "sampled") -> None:
        kind = "分钟" if is_minute_source(source) else "采样"
        title = f"{self.symbol} · {kind} {level}级" if self.symbol else ""
        self.chart.set_series(points, prev_close, title, level, source=source)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if self.symbol:
            self.activated.emit(self.symbol)


class MultiGridView(QWidget):
    cell_activated = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.grid = QGridLayout()
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(4)
        self.setLayout(self.grid)
        self.size = 4
        self.cells: list[GridCell] = []
        self._rebuild()

    def _rebuild(self) -> None:
        for cell in self.cells:
            cell.setParent(None)
            cell.deleteLater()
        self.cells = []
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()

        columns = {2: 2, 4: 2, 6: 3, 9: 3, 12: 4}.get(self.size, 3)
        for idx in range(self.size):
            cell = GridCell()
            cell.activated.connect(self.cell_activated)
            cell.chart.crosshair.connect(self._sync_crosshair)
            self.grid.addWidget(cell, idx // columns, idx % columns)
            self.cells.append(cell)
        for col in range(columns):
            self.grid.setColumnStretch(col, 1)
        for row in range((self.size + columns - 1) // columns):
            self.grid.setRowStretch(row, 1)

    def set_size(self, size: int) -> None:
        if size == self.size:
            return
        self.size = size
        self._rebuild()

    def set_symbols(self, symbols: list[str], names: dict[str, str]) -> None:
        for idx, cell in enumerate(self.cells):
            if idx < len(symbols):
                symbol = symbols[idx]
                cell.set_symbol(symbol, names.get(symbol, symbol.split(".")[0]))
            else:
                cell.set_symbol("", "")

    def symbols(self) -> list[str]:
        return [cell.symbol for cell in self.cells if cell.symbol]

    def update_quote(self, quote: Quote) -> None:
        for cell in self.cells:
            if cell.symbol == quote.symbol:
                cell.update_quote(quote)

    def update_series(self, symbol: str, points: list[SamplePoint],
                      prev_close: float | None, level: str,
                      source: str = "sampled") -> None:
        for cell in self.cells:
            if cell.symbol == symbol:
                cell.update_series(points, prev_close, level, source)

    def _sync_crosshair(self, slot: int) -> None:
        """多股共享时间轴：任一格移动光标，其余按同一分钟槽对齐。"""
        for cell in self.cells:
            if cell.chart is not self.sender():
                cell.chart.set_crosshair_slot(slot)
