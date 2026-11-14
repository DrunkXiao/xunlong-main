"""龙虎榜窗口：东方财富公开披露数据（C 级 · T 日盘后披露）。

设计口径：
- 只展示交易所公布的原因与营业部名称，**不做身份推断**（「机构专用」是官方席位名，原样显示）。
- 每条记录都带披露日与来源；抓取失败时明确报错，不用空列表冒充「当天没人上榜」。
- 表格里缺失值一律 `--`，不写 0。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget, QDialog,
)

from app.adapters.base import DataSourceError
from app.ui.panels import _label
from app.ui.theme import COLORS, color_for_change, format_amount, format_pct


class _NumCell(QTableWidgetItem):
    """带数值键的单元格。

    Qt 默认按显示文本排序，「-1704.05万」会排到「-289.00万」前面（字符串比较），
    所以数字列必须自己实现 __lt__，否则点表头排序结果是错的。
    """

    def __init__(self, text: str, key: float | None, color: str | None = None,
                 align_right: bool = True) -> None:
        super().__init__(text)
        self.setData(Qt.UserRole, key)
        self.setTextAlignment((Qt.AlignRight if align_right else Qt.AlignLeft) | Qt.AlignVCenter)
        if color:
            self.setForeground(QColor(color))

    def __lt__(self, other) -> bool:  # noqa: D105
        mine = self.data(Qt.UserRole)
        theirs = other.data(Qt.UserRole) if isinstance(other, QTableWidgetItem) else None
        if mine is None and theirs is None:
            return super().__lt__(other)
        return (mine if mine is not None else float("-inf")) < \
               (theirs if theirs is not None else float("-inf"))


def _TextCell(text: str, color: str | None = None, align_left: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment((Qt.AlignLeft if align_left else Qt.AlignRight) | Qt.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    return item


class LhbDialog(QDialog):
    watch_requested = Signal(list)      # 加入观察池
    symbol_activated = Signal(str)      # 双击榜单 → 主界面切换
    data_changed = Signal()             # 有新的披露数据落库（主界面据此刷新K线标记）

    def __init__(self, adapter, repo, current_symbol: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self.repo = repo
        self.current_symbol = current_symbol
        self._board: list[dict] = []
        self._seats_loaded: set[tuple[str, str]] = set()

        self.setWindowTitle("龙虎榜 · 东方财富公开披露数据")
        self.resize(1320, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addLayout(self._build_header())

        self.split = QSplitter(Qt.Horizontal)
        self.split.addWidget(self._build_board())
        self.split.addWidget(self._build_detail())
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 2)
        self.split.setSizes([780, 500])
        root.addWidget(self.split, 1)

        root.addLayout(self._build_footer())
        self.status = _label("", "muted")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._load()

    # ---------------------------------------------------------------- 骨架
    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)
        title = QLabel("龙虎榜")
        title.setProperty("role", "title")
        bar.addWidget(title)
        badge = _label("交易所披露 · C 级 · T 日盘后", "warn")
        badge.setToolTip("数据来源：data.eastmoney.com/stock/lhb.html\n"
                         "披露数据，非实时行情；不含任何资金身份推断")
        bar.addWidget(badge)
        bar.addSpacing(10)

        bar.addWidget(_label("交易日", "muted"))
        self.date_combo = QComboBox()
        self.date_combo.setMinimumWidth(120)
        self.date_combo.currentTextChanged.connect(self._on_date_changed)
        bar.addWidget(self.date_combo)

        self.btn_latest = QPushButton("取最新")
        self.btn_latest.setToolTip("向东方财富要最近一个有榜单的交易日（节假日自动跳过）")
        self.btn_latest.clicked.connect(lambda: self._load(force_latest=True))
        bar.addWidget(self.btn_latest)

        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(lambda: self._load(force_refresh=True))
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        return bar

    def _build_board(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(_label("当日上榜", "muted"))
        self.board_count = _label("", "muted")
        row.addWidget(self.board_count)
        row.addStretch(1)
        layout.addLayout(row)

        self.board_table = QTableWidget(0, 7)
        # 披露日放在标题行统一标注：每行都一样，单开一列只会把「上榜原因」挤没
        self.board_table.setHorizontalHeaderLabels(
            ["代码", "名称", "涨幅", "净买入", "买入额", "卖出额", "上榜原因"])
        self.board_table.verticalHeader().setVisible(False)
        self.board_table.verticalHeader().setDefaultSectionSize(21)
        self.board_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.board_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.board_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.board_table.setShowGrid(False)
        self.board_table.setSortingEnabled(True)
        head = self.board_table.horizontalHeader()
        for col, width in ((0, 86), (1, 76), (3, 88), (4, 80), (5, 82)):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
            self.board_table.setColumnWidth(col, width)
        head.setSectionResizeMode(2, QHeaderView.Fixed)
        self.board_table.setColumnWidth(2, 70)   # 「+20.00%」要占满，别被裁成 +20.0
        head.setSectionResizeMode(6, QHeaderView.Stretch)
        self.board_table.itemSelectionChanged.connect(self._on_board_selected)
        self.board_table.cellDoubleClicked.connect(self._on_board_double_clicked)
        layout.addWidget(self.board_table, 1)
        return wrap

    def _build_detail(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.detail_title = _label("该股上榜历史", "muted")
        head.addWidget(self.detail_title)
        head.addStretch(1)
        self.detail_badge = _label("", "muted")
        head.addWidget(self.detail_badge)
        layout.addLayout(head)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["日期", "净买入", "涨幅", "原因", "披露"])
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.verticalHeader().setDefaultSectionSize(21)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setShowGrid(False)
        hist_head = self.history_table.horizontalHeader()
        hist_head.setSectionResizeMode(0, QHeaderView.Fixed)
        self.history_table.setColumnWidth(0, 86)
        hist_head.setSectionResizeMode(1, QHeaderView.Fixed)
        self.history_table.setColumnWidth(1, 78)
        hist_head.setSectionResizeMode(2, QHeaderView.Fixed)
        self.history_table.setColumnWidth(2, 58)
        hist_head.setSectionResizeMode(3, QHeaderView.Stretch)
        hist_head.setSectionResizeMode(4, QHeaderView.Fixed)
        self.history_table.setColumnWidth(4, 82)
        layout.addWidget(self.history_table, 1)

        self.seat_tabs = QTabWidget()
        self.buy_table = self._seat_table()
        self.sell_table = self._seat_table()
        self.seat_tabs.addTab(self.buy_table, "买入前五")
        self.seat_tabs.addTab(self.sell_table, "卖出前五")
        self.seat_note = _label("", "muted")
        self.seat_note.setWordWrap(True)
        seat_wrap = QWidget()
        seat_layout = QVBoxLayout(seat_wrap)
        seat_layout.setContentsMargins(0, 0, 0, 0)
        seat_layout.setSpacing(2)
        seat_layout.addWidget(self.seat_tabs, 1)
        seat_layout.addWidget(self.seat_note)
        layout.addWidget(seat_wrap, 1)
        return wrap

    @staticmethod
    def _seat_table() -> QTableWidget:
        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["营业部", "买入", "卖出", "净额"])
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(21)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(True)
        head = table.horizontalHeader()
        head.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, width in ((1, 90), (2, 90), (3, 90)):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
            table.setColumnWidth(col, width)
        return table

    def _build_footer(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)
        hint = _label("数据为交易所披露口径，T 日盘后发布；营业部名称原样展示，不推断资金身份。", "muted")
        bar.addWidget(hint)
        bar.addStretch(1)
        self.btn_watch = QPushButton("加入龙虎榜观察池")
        self.btn_watch.clicked.connect(self._on_watch)
        bar.addWidget(self.btn_watch)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.accept)
        bar.addWidget(btn_close)
        return bar

    # ---------------------------------------------------------------- 取数
    def _wait(self, on: bool) -> None:
        if on:
            QApplication.setOverrideCursor(Qt.WaitCursor)
        else:
            QApplication.restoreOverrideCursor()

    def _load(self, force_latest: bool = False, force_refresh: bool = False) -> None:
        """优先走网络；网络失败退到库内已有日期，并明确告知用的是缓存。"""
        if self.adapter is None:
            self.status.setText("龙虎榜数据源未启用：检查配置 lhb.enabled / datasource.enable_lhb。")
            self._load_cached_only()
            return
        self._wait(True)
        try:
            if force_latest or not self.date_combo.count():
                try:
                    latest = self.adapter.latest_trade_date()
                except DataSourceError as exc:
                    latest = None
                    self._note_offline(str(exc))
                if latest:
                    self._ensure_date(latest)
            target = self.date_combo.currentText().strip()
            if not target:
                self.status.setText("没有可用的龙虎榜日期：网络不可用且库内为空。")
                return
            self._load_board(target, force_refresh=force_refresh)
        finally:
            self._wait(False)

    def _note_offline(self, message: str) -> None:
        self.status.setText(f"取最新失败，已退到本地缓存：{message}")

    def _load_cached_only(self) -> None:
        """没有可用数据源时，只展示已落库的历史，不假装能取数。"""
        dates = self.repo.lhb_dates(30)
        self.date_combo.blockSignals(True)
        self.date_combo.clear()
        for day in dates:
            self.date_combo.addItem(day)
        self.date_combo.blockSignals(False)
        if dates:
            self._load_board(dates[0])
        else:
            self._board = []
            self._fill_board([], "")
            self.status.setText("龙虎榜数据源未启用，且库内没有历史记录。")

    def _ensure_date(self, day: str) -> None:
        if self.date_combo.findText(day) >= 0:
            self.date_combo.setCurrentText(day)
            return
        self.date_combo.blockSignals(True)
        self.date_combo.insertItem(0, day)
        self.date_combo.setCurrentIndex(0)
        self.date_combo.blockSignals(False)

    def _load_board(self, day: str, force_refresh: bool = False) -> None:
        cached = self.repo.lhb_by_date(day)
        rows = cached
        if self.adapter is None:
            self._board = rows
            self._fill_board(rows, day)
            return
        if force_refresh or not cached:
            try:
                fresh = self.adapter.daily_board(day)
                if fresh:
                    self.repo.upsert_lhb(fresh)
                    self.data_changed.emit()
                    rows = self.repo.lhb_by_date(day)
                    self.status.setText(
                        f"{day} 抓取 {len(fresh)} 条，已落库（来源 {fresh[0]['source']}，"
                        f"披露日 {fresh[0]['publish_date']}）。")
                else:
                    self.status.setText(f"{day} 接口返回空：当天可能无上榜或尚未披露。")
            except DataSourceError as exc:
                if cached:
                    self.status.setText(f"网络失败，显示 {day} 的本地缓存 {len(cached)} 条。原因：{exc.message}")
                else:
                    self.status.setText(f"网络失败且无本地缓存：{exc.message}")
                    rows = []
        else:
            self.status.setText(f"{day} 本地缓存 {len(cached)} 条（点「刷新」可重新抓取）。")

        self._board = rows
        self._fill_board(rows, day)

    def _fill_board(self, rows: list[dict], day: str) -> None:
        self.board_table.setSortingEnabled(False)
        self.board_table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self.board_table.setItem(idx, 0, _TextCell(row["symbol"], COLORS["text_dim"]))
            self.board_table.setItem(idx, 1, _TextCell(row.get("name") or "--", COLORS["text"]))
            self.board_table.setItem(idx, 2, _NumCell(
                format_pct(row.get("change_pct")), row.get("change_pct"),
                color_for_change(row.get("change_pct"))))
            self.board_table.setItem(idx, 3, _NumCell(
                format_amount(row.get("net_buy")), row.get("net_buy"),
                color_for_change(row.get("net_buy"))))
            self.board_table.setItem(idx, 4, _NumCell(
                format_amount(row.get("buy_amount")), row.get("buy_amount"), COLORS["text_dim"]))
            self.board_table.setItem(idx, 5, _NumCell(
                format_amount(row.get("sell_amount")), row.get("sell_amount"), COLORS["text_dim"]))
            self.board_table.setItem(idx, 6, _TextCell(row.get("reason") or "--", COLORS["text_dim"]))
            tip = (f"{row['symbol']} {row.get('name') or ''}\n"
                   f"上榜原因：{row.get('reason') or '--'}\n"
                   f"净买入 {format_amount(row.get('net_buy'))} · "
                   f"买入 {format_amount(row.get('buy_amount'))} · 卖出 {format_amount(row.get('sell_amount'))}\n"
                   f"披露 {row.get('publish_date')} · 等级 {row.get('data_level') or 'C'} · "
                   f"来源 {row.get('source')}")
            self.board_table.item(idx, 0).setToolTip(tip)
        self.board_table.setSortingEnabled(True)
        published = rows[0].get("publish_date") if rows else None
        self.board_count.setText(
            (f"{len(rows)} 只 · 披露 {published}" if published
             else (f"{len(rows)} 只" if rows else "无记录")))
        if rows:
            self.board_table.sortItems(3, Qt.DescendingOrder)   # 默认按净买入降序
            self.board_table.selectRow(0)
        else:
            self._clear_detail()

    # ---------------------------------------------------------------- 详情
    def _current_row(self) -> dict | None:
        row_idx = self.board_table.currentRow()
        if 0 <= row_idx < len(self._board):
            symbol_item = self.board_table.item(row_idx, 0)
            if symbol_item is None:
                return None
            symbol = symbol_item.text()
            for row in self._board:
                if row["symbol"] == symbol:
                    return row
        return None

    def _on_board_selected(self) -> None:
        row = self._current_row()
        if not row:
            return
        self._fill_history(row["symbol"])
        self._fill_seats(row["symbol"], row.get("trade_date"))

    def _on_board_double_clicked(self, _row: int, _col: int) -> None:
        row = self._current_row()
        if row:
            self.symbol_activated.emit(row["symbol"])

    def _fill_history(self, symbol: str) -> None:
        rows = self.repo.lhb_for_symbol(symbol, 40)
        self.detail_title.setText(f"{symbol} 上榜历史（{len(rows)} 次）")
        self.detail_badge.setText("")
        self.history_table.setRowCount(len(rows))
        for idx, row in enumerate(rows):
            self.history_table.setItem(idx, 0, _TextCell(row["trade_date"], COLORS["text_dim"]))
            self.history_table.setItem(idx, 1, _NumCell(
                format_amount(row.get("net_buy")), row.get("net_buy"),
                color_for_change(row.get("net_buy"))))
            self.history_table.setItem(idx, 2, _NumCell(
                format_pct(row.get("change_pct")), row.get("change_pct"),
                color_for_change(row.get("change_pct"))))
            self.history_table.setItem(idx, 3, _TextCell(row.get("reason") or "--", COLORS["text_dim"]))
            self.history_table.setItem(idx, 4, _TextCell(row.get("publish_date") or "--",
                                                         COLORS["text_muted"]))

    def _fill_seats(self, symbol: str, trade_date: str | None) -> None:
        if not trade_date:
            self._show_seats([], [], "无日期，无法取席位。")
            return
        record = next((r for r in self.repo.lhb_for_symbol(symbol, 40)
                       if r["trade_date"] == trade_date), None)
        buy = (record or {}).get("buy_top5") or []
        sell = (record or {}).get("sell_top5") or []
        key = (symbol, trade_date)
        if not buy and not sell and key not in self._seats_loaded:
            self._seats_loaded.add(key)
            self._fetch_seats(symbol, trade_date)
            return
        self._show_seats(buy, sell)

    def _fetch_seats(self, symbol: str, trade_date: str) -> None:
        self.seat_note.setText(f"正在取 {trade_date} 席位…")
        self._wait(True)
        try:
            detail = self.adapter.fetch_detail(trade_date, symbol)
        except DataSourceError as exc:
            self._show_seats([], [], f"席位取数失败：{exc.message}")
            return
        finally:
            self._wait(False)
        if not detail["records"]:
            self._show_seats([], [], "该股当日没有榜单记录。")
            return
        self.repo.upsert_lhb(detail["records"])
        self.data_changed.emit()
        self._fill_history(symbol)
        self._show_seats(detail["buy"], detail["sell"],
                         institution_note=detail.get("institution_net"))

    def _show_seats(self, buy: list[dict], sell: list[dict], note: str = "",
                    institution_note: float | None = None) -> None:
        self._fill_seat_table(self.buy_table, buy)
        self._fill_seat_table(self.sell_table, sell)
        self.seat_tabs.setTabText(0, f"买入前五（{len(buy)}）")
        self.seat_tabs.setTabText(1, f"卖出前五（{len(sell)}）")
        if note:
            self.seat_note.setText(note)
            return
        parts = []
        if not buy and not sell:
            parts.append("库内暂无该日席位明细，点「刷新」或选中该行可重新抓取。")
        if institution_note is not None:
            parts.append(f"「机构专用」席位净额 {format_amount(institution_note)}"
                         "（官方席位名加总，不代表对资金身份的判断）")
        self.seat_note.setText("；".join(parts))

    def _fill_seat_table(self, table: QTableWidget, seats: list[dict]) -> None:
        table.setRowCount(len(seats))
        for idx, seat in enumerate(seats):
            name_cell = _TextCell(seat.get("name") or "--", COLORS["text"])
            name_cell.setToolTip(f"{seat.get('name') or '--'}\n"
                                 f"买入 {format_amount(seat.get('buy'))} · "
                                 f"卖出 {format_amount(seat.get('sell'))} · "
                                 f"净额 {format_amount(seat.get('net'))}")
            table.setItem(idx, 0, name_cell)
            table.setItem(idx, 1, _NumCell(format_amount(seat.get("buy")), seat.get("buy"),
                                           COLORS["text_dim"]))
            table.setItem(idx, 2, _NumCell(format_amount(seat.get("sell")), seat.get("sell"),
                                           COLORS["text_dim"]))
            table.setItem(idx, 3, _NumCell(format_amount(seat.get("net")), seat.get("net"),
                                           color_for_change(seat.get("net"))))

    def _clear_detail(self) -> None:
        self.detail_title.setText("该股上榜历史")
        self.history_table.setRowCount(0)
        self._show_seats([], [], "当天没有上榜记录。")

    # ---------------------------------------------------------------- 交互
    def _on_date_changed(self, day: str) -> None:
        day = (day or "").strip()
        if day:
            self.status.setText(f"切换交易日 {day}…")
            self._load_board(day)

    def _on_watch(self) -> None:
        row = self._current_row()
        if not row:
            QMessageBox.information(self, "龙虎榜", "先选一只上榜股票。")
            return
        self.watch_requested.emit([row["symbol"]])

    def set_status(self, text: str) -> None:
        self.status.setText(text)


def open_lhb_dialog(parent, adapter, repo, current_symbol: str | None = None) -> LhbDialog:
    """创建并显示窗口（调用方负责连信号）。"""
    dialog = LhbDialog(adapter, repo, current_symbol, parent)
    dialog.show()
    return dialog
