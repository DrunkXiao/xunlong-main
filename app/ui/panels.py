"""左栏（搜索/分组/股票列表）、右栏（盘口/预警）、底栏（日志/笔记）组件。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QModelIndex, QPoint, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCompleter, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMenu, QPlainTextEdit, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from app.adapters.minute import STOCKDB_FLAG
from app.db.models import Quote
from app.ui.theme import COLORS, color_for_change, format_amount, format_pct


def _num_item(text: str, color: str | None = None, align_right: bool = True) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment((Qt.AlignRight if align_right else Qt.AlignLeft) | Qt.AlignVCenter)
    if color:
        item.setForeground(QColor(color))
    return item


def _label(text: str, role: str = "") -> QLabel:
    label = QLabel(text)
    if role:
        label.setProperty("role", role)
    return label


class SearchBox(QWidget):
    """代码 / 名称 / 拼音首字母搜索。Enter 切单股，Ctrl+Enter 加入当前分组。"""

    picked = Signal(str)
    add_requested = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("代码 / 名称 / 拼音首字母，Enter 单股，Ctrl+Enter 加组")
        self.edit.setClearButtonEnabled(True)
        layout.addWidget(self.edit)

        self.model = QStringListModel()
        self.completer = QCompleter(self.model, self)
        self.completer.setCaseSensitivity(Qt.CaseInsensitive)
        self.completer.setFilterMode(Qt.MatchContains)
        self.completer.setMaxVisibleItems(12)
        self.completer.activated[str].connect(self._on_activated)
        self.edit.setCompleter(self.completer)

        self._map: dict[str, str] = {}
        self._results: list[str] = []
        self._universe = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._run_search)
        self.edit.textEdited.connect(lambda _: self._timer.start())
        self.edit.returnPressed.connect(self._on_enter)

    def set_universe(self, universe) -> None:
        self._universe = universe

    def _run_search(self) -> None:
        if self._universe is None:
            return
        text = self.edit.text().strip()
        if not text:
            self._results = []
            return
        rows = self._universe.search(text, limit=20)
        self._results = [row["symbol"] for row in rows]
        self._map = {
            f"{row['name']}  {row['code']}.{row['market']}": row["symbol"] for row in rows
        }
        self.model.setStringList(list(self._map))
        if rows:
            self.completer.complete()

    def _on_activated(self, text: str) -> None:
        symbol = self._map.get(text)
        if symbol:
            self.picked.emit(symbol)

    def _current_symbol(self) -> str | None:
        text = self.edit.text().strip().upper()
        if text in self._map:
            return self._map[text]
        for row_text, symbol in self._map.items():
            if text and (text in row_text.upper().replace(" ", "") or text == symbol):
                return symbol
        if self._results:
            return self._results[0]
        return None

    def _on_enter(self) -> None:
        symbol = self._current_symbol()
        if symbol:
            self.picked.emit(symbol)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            symbol = self._current_symbol()
            if symbol:
                if event.modifiers() & Qt.ControlModifier:
                    self.add_requested.emit(symbol)
                else:
                    self.picked.emit(symbol)
                return
        super().keyPressEvent(event)


class QuoteTable(QTableWidget):
    """组内股票列表：单击/双击精盯、可拖拽排序、Delete 移除。

    单击即打开「单股精盯」是主操作，最容易和拖拽排序打架，因此按下时记录位置，
    位移超过系统拖拽阈值就把本次释放判定为拖拽、不当作点击（见 `_on_clicked`）。
    """

    activated = Signal(str)          # 双击 / 回车：打开单股精盯
    open_requested = Signal(str)     # 单击某行：打开单股精盯（拖拽不触发）
    reordered = Signal(list)
    remove_requested = Signal(list)
    add_requested = Signal(str)

    COLUMNS = ["名称", "代码", "最新价", "涨跌幅", "成交额"]

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(44)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(1, 68)
        for col, width in ((2, 60), (3, 58), (4, 58)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            self.setColumnWidth(col, width)
        self.doubleClicked.connect(self._on_double_click)
        self.clicked.connect(self._on_clicked)
        self.model().rowsMoved.connect(self._on_rows_moved)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setToolTip("单击或双击任意一只股票 → 打开单股精盯；拖拽可调整顺序；Del 移除")

        self._symbols: list[str] = []
        self._quotes: dict[str, Quote] = {}
        # 单击 vs 拖拽排序的判定状态
        self._press_pos: QPoint | None = None
        self._dragged = False
        self._suppress_click = False

    # ------------------------------------------------------------ 数据
    def symbols(self) -> list[str]:
        return list(self._symbols)

    def set_symbols(self, symbols: list[str], names: dict[str, str] | None = None) -> None:
        names = names or {}
        self._symbols = list(symbols)
        self.setRowCount(len(symbols))
        for row, symbol in enumerate(symbols):
            code, market = symbol.split(".", 1) if "." in symbol else (symbol, "")
            self.setItem(row, 0, _num_item(names.get(symbol, code), None, align_right=False))
            self.setItem(row, 1, _num_item(f"{code}.{market}", COLORS["text_dim"], align_right=False))
            for col in (2, 3, 4):
                self.setItem(row, col, _num_item("--", COLORS["text_muted"]))
            self.item(row, 1).setData(Qt.UserRole, symbol)
        for symbol, quote in self._quotes.items():
            if symbol in self._symbols:
                self.update_quote(quote)

    def update_quote(self, quote: Quote) -> None:
        self._quotes[quote.symbol] = quote
        try:
            row = self._symbols.index(quote.symbol)
        except ValueError:
            return
        if row >= self.rowCount():
            return
        pct = quote.change_pct
        color = color_for_change(pct)
        self.setItem(row, 2, _num_item(f"{quote.price:.2f}" if quote.price is not None else "--", color))
        self.setItem(row, 3, _num_item(format_pct(pct), color))
        self.setItem(row, 4, _num_item(format_amount(quote.amount), COLORS["text_dim"]))
        if not self.item(row, 0).text() or self.item(row, 0).text() == quote.symbol.split(".")[0]:
            if quote.name:
                self.setItem(row, 0, _num_item(quote.name, None, align_right=False))

    def select_symbol(self, symbol: str) -> None:
        if symbol in self._symbols:
            self.selectRow(self._symbols.index(symbol))

    # ------------------------------------------------------------ 交互
    def _on_clicked(self, index: QModelIndex) -> None:
        """单击行 → 打开单股精盯。拖拽排序结束时的释放不算点击。"""
        if self._suppress_click or not index.isValid():
            return
        if 0 <= index.row() < len(self._symbols):
            self.open_requested.emit(self._symbols[index.row()])

    def _on_double_click(self, index: QModelIndex) -> None:
        if 0 <= index.row() < len(self._symbols):
            self.activated.emit(self._symbols[index.row()])

    # 拖拽排序用的是左键长按，和「单击打开」同一条链路：按下记位置，移动超过系统
    # 阈值即标脏，释放时 Qt 仍可能补发 clicked，用 _suppress_click 挡掉。
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self._dragged = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if (self._press_pos is not None and not self._dragged
                and event.buttons() & Qt.LeftButton):
            offset = event.position().toPoint() - self._press_pos
            if offset.manhattanLength() >= QApplication.startDragDistance():
                self._dragged = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._suppress_click = self._dragged      # clicked 在 super() 内部才发出
        super().mouseReleaseEvent(event)
        self._suppress_click = False
        self._dragged = False
        self._press_pos = None

    def _on_rows_moved(self, parent, start, end, dest, dest_row) -> None:
        symbols = []
        for row in range(self.rowCount()):
            item = self.item(row, 1)
            if item is None:
                continue
            symbols.append(item.data(Qt.UserRole) or item.text())
        if len(symbols) == len(self._symbols):
            self._symbols = symbols
            self.reordered.emit(symbols)

    def _show_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if row < 0 or row >= len(self._symbols):
            return
        symbol = self._symbols[row]
        menu = QMenu(self)
        open_action = QAction("单股精盯", self)
        open_action.triggered.connect(lambda: self.activated.emit(symbol))
        remove_action = QAction("从当前分组移除", self)
        remove_action.triggered.connect(lambda: self.remove_requested.emit([symbol]))
        menu.addAction(open_action)
        menu.addSeparator()
        menu.addAction(remove_action)
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            rows = {index.row() for index in self.selectedIndexes()}
            symbols = [self._symbols[r] for r in sorted(rows) if r < len(self._symbols)]
            if symbols:
                self.remove_requested.emit(symbols)
                return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            rows = {index.row() for index in self.selectedIndexes()}
            for row in sorted(rows):
                if row < len(self._symbols):
                    self.activated.emit(self._symbols[row])
                    return
        super().keyPressEvent(event)


class GroupPanel(QWidget):
    group_changed = Signal(int)
    new_group_requested = Signal(str)
    rename_requested = Signal(int, str)
    copy_requested = Signal(int, str)
    delete_requested = Signal(int)
    order_changed = Signal(list)
    picked = Signal(str)
    add_requested = Signal(str)
    remove_requested = Signal(list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.search = SearchBox()
        self.search.picked.connect(self.picked)
        self.search.add_requested.connect(self.add_requested)
        layout.addWidget(self.search)

        header = QHBoxLayout()
        header.setSpacing(4)
        header.addWidget(_label("自选分组", "muted"))
        header.addStretch(1)
        for text, tip, slot in (
            ("📄", "新建分组", self._on_new),
            ("✏️", "重命名当前分组", self._on_rename),
            ("📋", "复制当前分组", self._on_copy),
            ("🗑️", "删除当前分组（需二次确认）", self._on_delete),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setFixedWidth(42)
            btn.clicked.connect(slot)
            header.addWidget(btn)
        layout.addLayout(header)

        self.group_list = QListWidget()
        self.group_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.group_list.setDefaultDropAction(Qt.MoveAction)
        self.group_list.setMaximumHeight(230)
        self.group_list.currentItemChanged.connect(self._on_group_changed)
        self.group_list.model().rowsMoved.connect(self._on_groups_moved)
        layout.addWidget(self.group_list)

        layout.addWidget(_label("组内股票（单击即精盯，拖拽排序，Del 移除）", "muted"))
        self.table = QuoteTable()
        self.table.activated.connect(self.picked)
        self.table.open_requested.connect(self.picked)
        self.table.reordered.connect(self.order_changed)
        self.table.remove_requested.connect(self.remove_requested)
        layout.addWidget(self.table, 1)

        self.hint = _label("", "muted")
        self.hint.setWordWrap(True)
        layout.addWidget(self.hint)

    # ------------------------------------------------------------ 分组
    def load_groups(self, groups: list[dict], current_id: int | None = None) -> None:
        self.group_list.blockSignals(True)
        self.group_list.clear()
        for group in groups:
            item = QListWidgetItem(f"{group['name']}  ({group['count']})")
            item.setData(Qt.UserRole, group["id"])
            item.setData(Qt.UserRole + 1, group["name"])
            self.group_list.addItem(item)
        self.group_list.blockSignals(False)
        if current_id is not None:
            for row in range(self.group_list.count()):
                if self.group_list.item(row).data(Qt.UserRole) == current_id:
                    self.group_list.setCurrentRow(row)
                    return
        if self.group_list.count():
            self.group_list.setCurrentRow(0)

    def current_group_id(self) -> int | None:
        item = self.group_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def current_group_name(self) -> str:
        item = self.group_list.currentItem()
        return item.data(Qt.UserRole + 1) if item else ""

    def _on_group_changed(self, current: QListWidgetItem | None, previous) -> None:
        if current is None:
            return
        self.group_changed.emit(int(current.data(Qt.UserRole)))

    def _on_groups_moved(self, *args) -> None:
        order = [self.group_list.item(i).data(Qt.UserRole) for i in range(self.group_list.count())]
        self.order_changed.emit([int(gid) for gid in order if gid is not None])

    def _on_new(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, "新建分组", "分组名称")
        if ok and name.strip():
            self.new_group_requested.emit(name.strip())

    def _on_rename(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        item = self.group_list.currentItem()
        if item is None:
            return
        name, ok = QInputDialog.getText(self, "重命名分组", "新的名称",
                                        text=item.data(Qt.UserRole + 1))
        if ok and name.strip():
            self.rename_requested.emit(int(item.data(Qt.UserRole)), name.strip())

    def _on_copy(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        item = self.group_list.currentItem()
        if item is None:
            return
        default = f"{item.data(Qt.UserRole + 1)} 副本"
        name, ok = QInputDialog.getText(self, "复制分组", "新分组名称", text=default)
        if ok and name.strip():
            self.copy_requested.emit(int(item.data(Qt.UserRole)), name.strip())

    def _on_delete(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        item = self.group_list.currentItem()
        if item is None:
            return
        count = self.table.rowCount()
        text = f"删除分组「{item.data(Qt.UserRole + 1)}」？"
        if count:
            text += f"\n该分组下有 {count} 只股票，删除后分组与组内记录一并清除（股票本身不受影响）。"
        if QMessageBox.question(self, "确认删除", text) == QMessageBox.Yes:
            self.delete_requested.emit(int(item.data(Qt.UserRole)))

    def set_stock_hint(self, text: str) -> None:
        self.hint.setText(text)


class OrderBookPanel(QWidget):
    """五档盘口。数据源不支持时显示原因，不留空壳。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        header = QHBoxLayout()
        header.addWidget(_label("五档盘口", "muted"))
        header.addStretch(1)
        self.badge = _label("", "muted")
        header.addWidget(self.badge)
        layout.addLayout(header)
        self.table = QTableWidget(10, 3)
        self.table.setHorizontalHeaderLabels(["档位", "价格", "手数"])
        vh = self.table.verticalHeader()
        vh.setVisible(False)
        vh.setSectionResizeMode(QHeaderView.Fixed)
        vh.setDefaultSectionSize(20)
        vh.setMinimumSectionSize(16)
        self._freeze_rows()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setShowGrid(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)
        self.note = _label("", "muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    def _freeze_rows(self, height: int = 20) -> None:
        """逐行锁死行高。QSS 给 ::item 留了 2px 上下内边距，Qt 会把默认行高推到 25，
        10 档就要 250px，280px 的面板装不下（买4/买5 被裁）。setRowHeight 逐行设置才不被覆盖。"""
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, height)

    def set_order_book(self, book, prev_close: float | None) -> None:
        rows = []
        for level in range(5, 0, -1):
            idx = level - 1
            price, volume = book.asks[idx] if idx < len(book.asks) else (None, None)
            rows.append((f"卖{level}", price, volume))
        for level in range(1, 6):
            idx = level - 1
            price, volume = book.bids[idx] if idx < len(book.bids) else (None, None)
            rows.append((f"买{level}", price, volume))
        for row, (name, price, volume) in enumerate(rows):
            color = color_for_change((price - prev_close) if (price and prev_close) else None)
            self.table.setItem(row, 0, _num_item(name, COLORS["text_dim"], align_right=False))
            self.table.setItem(row, 1, _num_item(f"{price:.2f}" if price else "--", color))
            self.table.setItem(row, 2, _num_item(f"{volume}" if volume else "--", COLORS["text_dim"]))
        self.badge.setText(book.source if hasattr(book, "source") else "")
        self.note.setText("")
        self._freeze_rows()

    def set_unsupported(self, reason: str) -> None:
        self.table.setRowCount(0)
        self.badge.setText("不支持")
        self.note.setText(reason)


class SampledTradeTable(QTableWidget):
    """采样成交明细。列名写「区间」，行内带「推演」意思，绝不冒充逐笔。"""

    row_selected = Signal(object)     # datetime

    COLUMNS = ["时间", "区间量", "区间额", "方向"]
    MAX_ROWS = 300

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(19)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setShowGrid(False)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(38)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.setColumnWidth(0, 62)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(1, 56)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        self.setColumnWidth(3, 52)
        self.cellClicked.connect(self._on_click)
        self._points: list = []
        self._suppress = False

    # ------------------------------------------------------------ 数据
    def set_points(self, points: list) -> None:
        self._suppress = True
        self._points = list(points)[-self.MAX_ROWS:]
        self.setRowCount(len(self._points))
        for row, point in enumerate(self._points):
            self._fill(row, point)
        self._suppress = False
        if self._points:
            self.scrollToBottom()

    def append_point(self, point) -> None:
        if self._points and self._points[-1].time >= point.time:
            return
        self._suppress = True
        self._points.append(point)
        if len(self._points) > self.MAX_ROWS:
            drop = len(self._points) - self.MAX_ROWS
            self._points = self._points[drop:]
            self.setRowCount(len(self._points))
            for row, item in enumerate(self._points):
                self._fill(row, item)
        else:
            row = self.rowCount()
            self.setRowCount(row + 1)
            self._fill(row, point)
        self._suppress = False
        self.scrollToBottom()

    def _fill(self, row: int, point) -> None:
        flag = point.quality_flag or ""
        no_update = "no_update" in flag                    # 快照没更新：断点，不是零成交
        no_trade = "no_trade" in flag                      # 分钟线里真实的零成交分钟
        minute_source = "em_minute" in flag or STOCKDB_FLAG in flag
        color = (COLORS["up"] if point.direction == "buy"
                 else COLORS["down"] if point.direction == "sell" else COLORS["text_muted"])
        direction = {"buy": "偏买", "sell": "偏卖"}.get(point.direction, "不明")
        if no_update:
            direction = "无更新"
        elif no_trade:
            direction = "无成交"
        # 快照没更新时不能写 0：那一格会被读成「零成交」。按铁律写 -- 并在提示里说明。
        # 东财分钟线的 0 是真的没撮合，可以写 0，但要说清是零成交而不是缺数据。
        blank = no_update
        vol_text = "--" if blank else f"{point.interval_volume / 100:.0f}"
        amt_text = "--" if blank else format_amount(point.interval_amount)
        self.setItem(row, 0, _num_item(point.time.strftime("%H:%M:%S"), COLORS["text_dim"], False))
        self.setItem(row, 1, _num_item(vol_text, COLORS["text_dim"]))
        self.setItem(row, 2, _num_item(amt_text, color if not blank else COLORS["text_muted"]))
        self.setItem(row, 3, _num_item(direction, color if not blank else COLORS["text_muted"], False))

        how = ("free-stockdb 本地库 1 分钟聚合，非逐笔" if STOCKDB_FLAG in flag
               else "东方财富 1 分钟聚合，非逐笔" if minute_source
               else "快照差分推演，非逐笔")
        span = f"{point.interval_span_sec:.0f}s" if point.interval_span_sec else "—"
        avg = f"{point.avg_price:.3f}" if point.avg_price else "--"
        self.item(row, 0).setToolTip(
            f"{point.time.isoformat(timespec='seconds')} · 区间 {span}\n"
            f"区间量 {point.interval_volume} 股 / 区间额 {point.interval_amount:.0f} 元\n"
            f"累计量 {point.cumulative_volume} 股 · 均价 {avg}\n"
            f"质量标记 {point.quality_flag}（{how}）")
        if no_update:
            self.item(row, 3).setToolTip("本区间快照未更新，没有新增成交；这是断点，不是零成交。")
        elif no_trade:
            self.item(row, 3).setToolTip("该分钟交易所没有撮合成交（量额为 0），不是缺数据。")

    # ------------------------------------------------------------ 联动
    def _on_click(self, row: int, _col: int) -> None:
        if self._suppress or not (0 <= row < len(self._points)):
            return
        self.row_selected.emit(self._points[row].time)

    def select_time(self, moment) -> None:
        """外部（分时点击）定位：选最近的一行并滚动可见。"""
        if not self._points:
            return
        nearest = min(range(len(self._points)),
                      key=lambda i: abs((self._points[i].time - moment).total_seconds()))
        self._suppress = True
        self.selectRow(nearest)
        item = self.item(nearest, 0)
        if item is not None:
            self.scrollToItem(item, QAbstractItemView.PositionAtCenter)
        self._suppress = False

    def times(self) -> list:
        return [p.time for p in self._points]


class LargeOrderTable(QTableWidget):
    """区间大单：档位基于区间累计成交额，不是单笔成交额。"""

    row_selected = Signal(object)     # LargeOrder

    COLUMNS = ["时间", "档位", "区间额", "标签"]
    MAX_ROWS = 200

    def __init__(self, parent=None) -> None:
        super().__init__(0, len(self.COLUMNS), parent)
        self.setHorizontalHeaderLabels(self.COLUMNS)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(19)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setShowGrid(False)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setMinimumSectionSize(38)
        self.setFont(QFont("Consolas", 8))
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.setColumnWidth(0, 62)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        self.setColumnWidth(1, 42)
        header.setSectionResizeMode(2, QHeaderView.Fixed)
        self.setColumnWidth(2, 64)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.cellClicked.connect(self._on_click)
        self._orders: list = []

    def set_orders(self, orders: list) -> None:
        self._orders = list(orders)[-self.MAX_ROWS:]
        self.setRowCount(len(self._orders))
        for row, order in enumerate(self._orders):
            self._fill(row, order)
        if self._orders:
            self.scrollToBottom()

    def append_order(self, order) -> None:
        self._orders.append(order)
        if len(self._orders) > self.MAX_ROWS:
            self._orders = self._orders[-self.MAX_ROWS:]
            self.set_orders(self._orders)
            return
        row = self.rowCount()
        self.setRowCount(row + 1)
        self._fill(row, order)
        self.scrollToBottom()

    def _fill(self, row: int, order) -> None:
        color = (COLORS["up"] if order.direction == "buy"
                 else COLORS["down"] if order.direction == "sell" else COLORS["warn"])
        self.setItem(row, 0, _num_item(order.time.strftime("%H:%M:%S"),
                                       COLORS["text_dim"], False))
        self.setItem(row, 1, _num_item(order.tier_short, color, False))
        self.setItem(row, 2, _num_item(format_amount(order.amount), color))
        tag_item = _num_item(order.tag, COLORS["text"], False)
        tag_item.setToolTip(
            f"{order.tier_label} · {order.direction_label} · {order.position_label}\n"
            f"区间累计 {order.amount / 1e4:.0f} 万元 / 阈值 {order.threshold / 1e4:.0f} 万元\n"
            f"区间跨度 {order.window_sec:.0f}s · 数据等级 {order.data_level}（采样推演，非单笔）")
        self.setItem(row, 3, tag_item)

    def _on_click(self, row: int, _col: int) -> None:
        if 0 <= row < len(self._orders):
            self.row_selected.emit(self._orders[row])

    def orders(self) -> list:
        return list(self._orders)


class TradePanel(QWidget):
    """右栏成交区：采样成交与区间大单两张表，口径写在标题上。"""

    time_focused = Signal(object)     # datetime

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.title_label = _label("成交（区间采样）", "muted")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.badge = _label("非逐笔", "warn")
        self.badge.setToolTip("数据源无逐笔接口，明细由相邻快照差分推演")
        header.addWidget(self.badge)
        layout.addLayout(header)

        self.caption = _label("快照差分推演，非逐笔成交", "muted")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)

        self._source_mode = "sampled"
        self.tabs = QTabWidget()
        self.trades = SampledTradeTable()
        self.orders = LargeOrderTable()
        self.tabs.addTab(self.trades, "采样成交")
        self.tabs.addTab(self.orders, "区间大单")

        tick_wrap = QWidget()
        tick_layout = QVBoxLayout(tick_wrap)
        tick_layout.setContentsMargins(4, 4, 4, 4)
        self.tick_note = QLabel("")
        self.tick_note.setWordWrap(True)
        self.tick_note.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.tick_note.setProperty("role", "muted")
        tick_layout.addWidget(self.tick_note)
        tick_layout.addStretch(1)
        self.tabs.addTab(tick_wrap, "逐笔")

        layout.addWidget(self.tabs, 1)

        self.trades.row_selected.connect(self.time_focused)
        self.orders.row_selected.connect(lambda order: self.time_focused.emit(order.time))

    def set_tick_supported(self, supported: bool, reason: str = "") -> None:
        """逐笔面板：支持就变成可用页签，不支持就写清原因与替代口径。"""
        self.tabs.setTabText(2, "逐笔" if supported else "逐笔(不支持)")
        if supported:
            self.tick_note.setText("该数据源支持逐笔成交，明细将在此显示。")
            return
        self.tick_note.setText(
            reason or
            "数据源不提供逐笔成交。\n\n"
            "替代口径：\n"
            "· 采样成交页按轮询间隔做相邻快照差分，标「区间」；\n"
            "· 区间大单页按区间累计成交额分档，标「区间累计」；\n"
            "· 两者都不是逐笔流水，不能用于判断单笔委托大小。")

    def set_source_mode(self, mode: str) -> None:
        """切换成交口径的展示文案：em=东财分钟线，sampled=快照差分。口径必须写在标题上。"""
        self._source_mode = mode
        if mode == "em":
            self.title_label.setText("成交（分钟区间）")
            self.tabs.setTabText(0, "分钟成交")
            self.badge.setText("非逐笔")
            self.badge.setToolTip("东方财富 1 分钟聚合成交，不是逐笔流水")
            self.caption.setText(
                "东财 1 分钟聚合：区间量/区间额 = 该分钟成交量/成交额；"
                "方向由「本分钟收价 vs 上一分钟收价」推演，非内外盘")
            return
        self.title_label.setText("成交（区间采样）")
        self.tabs.setTabText(0, "采样成交")
        self.badge.setToolTip("数据源无逐笔接口，明细由相邻快照差分推演")
        self.caption.setText("快照差分推演，非逐笔成交")

    def set_interval(self, seconds: float) -> None:
        if getattr(self, "_source_mode", "sampled") == "em":
            return          # 分钟线不依赖轮询间隔，别用采样口径覆盖标题
        self.caption.setText(f"区间 = 轮询间隔 {seconds:.0f}s，相邻快照差分推演，非逐笔成交")

    def set_points(self, points: list) -> None:
        self.trades.set_points(points)

    def append_point(self, point) -> None:
        self.trades.append_point(point)

    def set_orders(self, orders: list) -> None:
        self.orders.set_orders(orders)

    def append_order(self, order) -> None:
        self.orders.append_order(order)

    def select_time(self, moment) -> None:
        self.trades.select_time(moment)

    def clear(self) -> None:
        self.trades.set_points([])
        self.orders.set_orders([])

    def set_unsupported(self, reason: str) -> None:
        self.badge.setText("不支持")
        self.caption.setText(reason)


class AlertRulePanel(QWidget):
    """当前股票的预警规则列表。"""

    add_requested = Signal(str)
    delete_requested = Signal(int)
    toggle_requested = Signal(int, bool)
    refresh_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        header = QHBoxLayout()
        header.setSpacing(4)
        header.addWidget(_label("预警规则", "muted"))
        header.addStretch(1)
        self.btn_add = QPushButton("＋预警")
        self.btn_add.setFixedWidth(58)
        self.btn_add.setToolTip("为当前股票新增价格/涨幅预警")
        self.btn_add.clicked.connect(lambda: self.add_requested.emit(""))
        header.addWidget(self.btn_add)
        btn_refresh = QPushButton("刷新")
        btn_refresh.setFixedWidth(42)
        btn_refresh.setToolTip("重新载入规则")
        btn_refresh.clicked.connect(self.refresh_requested)
        header.addWidget(btn_refresh)
        layout.addLayout(header)

        tip = _label("只提示条件，不构成买卖建议", "muted")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["规则", "条件", "状态"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setShowGrid(False)
        header_view = self.table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.table)

    def set_rules(self, rules: list[dict]) -> None:
        self.table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            params = rule.get("params") or {}
            cond = " ".join(f"{k}={v}" for k, v in params.items()) or "-"
            self.table.setItem(row, 0, _num_item(
                f"#{rule['id']} {rule.get('name', '')}", None, align_right=False))
            self.table.setItem(row, 1, _num_item(cond, COLORS["text_dim"], align_right=False))
            item = _num_item("启用" if rule.get("enabled") else "停用",
                             COLORS["up"] if rule.get("enabled") else COLORS["text_muted"],
                             align_right=False)
            item.setData(Qt.UserRole, int(rule["id"]))
            item.setData(Qt.UserRole + 1, bool(rule.get("enabled")))
            self.table.setItem(row, 2, item)

    def _show_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        item = self.table.item(row, 2)
        if item is None:
            return
        rule_id = int(item.data(Qt.UserRole))
        enabled = bool(item.data(Qt.UserRole + 1))
        menu = QMenu(self)
        toggle = QAction("停用" if enabled else "启用", self)
        toggle.triggered.connect(lambda: self.toggle_requested.emit(rule_id, not enabled))
        remove = QAction("删除规则", self)
        remove.triggered.connect(lambda: self.delete_requested.emit(rule_id))
        menu.addAction(toggle)
        menu.addAction(remove)
        menu.exec(self.table.viewport().mapToGlobal(pos))


class LogPanel(QWidget):
    note_saved = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()

        self.alerts = QPlainTextEdit(readOnly=True)
        self.alerts.setMaximumBlockCount(2000)
        self.events = QPlainTextEdit(readOnly=True)
        self.events.setMaximumBlockCount(2000)
        self.events.setPlainText(
            "异动记录：价格/量能预警与区间大单会写在这里。\n"
            "口径提示：逐笔不可用时，大单按「采样区间累计成交额」分档，不是单笔成交。")

        note_wrap = QWidget()
        note_layout = QVBoxLayout(note_wrap)
        note_layout.setContentsMargins(2, 2, 2, 2)
        self.note = QTextEdit()
        self.note.setPlaceholderText("复盘笔记：写下对该股的观察。保存后写入 review_notes，可追溯。")
        btn_save = QPushButton("保存笔记")
        btn_save.clicked.connect(self._on_save)
        note_layout.addWidget(self.note, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(btn_save)
        note_layout.addLayout(row)

        self.tabs.addTab(self.alerts, "预警日志")
        self.tabs.addTab(self.events, "异动记录")
        self.tabs.addTab(note_wrap, "复盘笔记")
        layout.addWidget(self.tabs)

    def append_alert(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.alerts.appendPlainText(f"[{stamp}] {text}")

    def append_event(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.events.appendPlainText(f"[{stamp}] {text}")

    def _on_save(self) -> None:
        self.note_saved.emit(self.note.toPlainText().strip())
