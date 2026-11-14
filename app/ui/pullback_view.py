"""强势回调低吸视图：全市场扫描 → 候选表 → 交易计划与日K复核。

界面口径（与 README 铁律一致）：

- 顶栏永远写清**数据日**与**"日线收盘、非实时"**：这是收盘后才成立的信号，
  不是盘中实时选股，冒充实时会让人在盘中按盘后信号下单；
- 候选表只排「确认 / 观察」，剔除的股票不占版面，但「未通过原因统计」照实给，
  不把"没有标的"说成"市场没有机会"；
- 交易计划里的股数、风险额、目标价都标明计算口径（买入区间上沿），
  价位线画在日K上是"计划"，不是成交事实；
- 数据过期（超过 stale_days_max）时确认信号一律降为观察，并在顶部写明原因。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
    QLabel, QMessageBox, QProgressBar, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.services.pullback import CHECKS, SCORE_PARTS, SIGNAL_LABEL, Pick, ScanResult
from app.services.pullback_report import render as render_report
from app.services.pullback_scanner import PullbackScanner, ScanDone, ScanProgress
from app.ui.charts import CandleChart
from app.ui.panels import _label
from app.ui.tdx_link_button import TdxLinkButton
from app.ui.theme import COLORS, color_for_change

SETTING_KEY = "pullback.last_scan"
FILTERS = (("all", "全部候选"), ("buy", "仅确认"), ("watch", "仅观察"))

# 未通过原因在底部一行里只能写短名，完整条件见 tooltip
SHORT_CHECK = {
    "limitup_10d": "涨停", "stage_high_10d": "新高", "trend_ok": "趋势",
    "support_ok": "支撑", "volume_ok": "缩量", "candle_ok": "止跌",
    "no_distribution": "无长阴", "no_trend_break": "未破MA20",
}

BADGE_TIP = (
    "信号只在收盘后成立，盘中不确认阳包阴/锤子线。\n"
    "数据来自本地通达信日线文件（vipdoc/*/lday），价格为不复权。\n"
    "等级 C：历史口径，不是实时行情。")

COLUMNS = (
    ("#", 32), ("代码", 58), ("名称", 64), ("行业", 70), ("评分", 40), ("信号", 50),
    ("收盘", 56), ("缩量比", 56), ("涨停日", 84), ("止损", 54), ("2.5R目标", 64),
)


class _NumCell(QTableWidgetItem):
    """带数值键的单元格：Qt 默认按文本排序，会把 100 排到 99 前面。"""

    def __init__(self, text: str, key, color: str | None = None, align_right: bool = True) -> None:
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


def _best_of(value, digits: int = 2) -> str:
    return "--" if value is None else f"{float(value):.{digits}f}"


class PullbackView(QWidget):
    """主界面第三块：收盘后选股。数据来自本地通达信日线，不占 tqcenter 会话。"""

    symbol_activated = Signal(str)      # 双击候选 → 主界面切到该股
    add_requested = Signal(list)        # 加入「强势回调池」分组
    bars_needed = Signal(str)           # 需要该股日K（恢复上次结果时内存里没有）
    message = Signal(str)               # 需要写进右下角日志的文案

    def __init__(self, config: dict, repo, parent=None) -> None:
        super().__init__(parent)
        self.config = config or {}
        self.repo = repo
        self.result: ScanResult | None = None
        self.picks: list[Pick] = []
        self._rows: list[Pick] = []
        self._current: Pick | None = None
        self.scanner = PullbackScanner(config)

        self._build()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(400)

        self.scanner.start()
        self._restore()

    # ================================================================ 骨架
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(4)
        layout.addLayout(self._build_header())
        layout.addLayout(self._build_stats())

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._build_table())
        split.addWidget(self._build_detail())
        # 候选表要放下 11 列（固定列宽合计约 540），所以左侧至少给到 700；
        # 右侧详情里计划卡是 3 列网格，500 左右就够，剩下的宽度都让给表格。
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([700, 520])
        layout.addWidget(split, 1)
        layout.addWidget(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        title = QLabel("强势回调低吸")
        title.setProperty("role", "title")
        bar.addWidget(title)

        self.badge = _label("日线收盘 · 非实时", "warn")
        self.badge.setToolTip(BADGE_TIP)
        bar.addWidget(self.badge)

        self.lbl_data = _label("数据日 --", "muted")
        bar.addWidget(self.lbl_data)

        bar.addWidget(_label("筛选", "muted"))
        self.combo = QComboBox()
        for key, text in FILTERS:
            self.combo.addItem(text, key)
        self.combo.currentIndexChanged.connect(lambda _: self._fill_table())
        bar.addWidget(self.combo)
        bar.addStretch(1)

        self.btn_scan = QPushButton("扫描全市场")
        self.btn_scan.setProperty("accent", True)
        self.btn_scan.setToolTip("读本地日线逐只评估，约 1 分钟；扫描期间可继续盯盘")
        self.btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_scan)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.scanner.request_stop)
        bar.addWidget(self.btn_stop)

        self.btn_export = QPushButton("导出报告")
        self.btn_export.setToolTip("导出单文件 HTML（含市场环境、交易计划、未通过原因统计）")
        self.btn_export.clicked.connect(self._export)
        bar.addWidget(self.btn_export)
        return bar

    def _build_stats(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.lbl_stats = _label("尚未扫描。点右上角「扫描全市场」，用本地日线跑一遍。", "muted")
        row.addWidget(self.lbl_stats)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(180)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        row.addWidget(self.progress)
        row.addStretch(1)
        return row

    def _build_table(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(21)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setSortingEnabled(True)
        self.table.setMinimumWidth(560)
        head = self.table.horizontalHeader()
        for col, (_, width) in enumerate(COLUMNS):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        head.setSectionResizeMode(3, QHeaderView.Stretch)
        # 行序就是策略排名（确认 > 观察 > 剔除）；这里把排序指示器固定在「#」
        # 并显式升序，否则 Qt 打开排序时会按上一次的降序把最后一名排到第一行。
        head.setSortIndicator(0, Qt.AscendingOrder)
        head.setSortIndicatorShown(True)
        self.table.itemSelectionChanged.connect(self._on_selected)
        self.table.cellDoubleClicked.connect(self._on_activated)
        layout.addWidget(self.table, 1)
        self.lbl_table_hint = _label("双击行可在单股视图打开该股", "muted")
        layout.addWidget(self.lbl_table_hint)
        return wrap

    def _build_detail(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.setContentsMargins(0, 0, 6, 0)     # 别让最右侧按钮贴着面板边线
        self.detail_title = QLabel("未选择标的")
        self.detail_title.setFont(QFont("Microsoft YaHei UI", 12, QFont.DemiBold))
        head.addWidget(self.detail_title)
        self.detail_badge = _label("", "muted")
        head.addWidget(self.detail_badge)
        head.addStretch(1)
        self.btn_add = QPushButton("加入选股池")
        self.btn_add.setEnabled(False)
        self.btn_add.clicked.connect(self._on_add)
        head.addWidget(self.btn_add)
        self.btn_open = QPushButton("单股查看")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._on_open)
        head.addWidget(self.btn_open)
        self.btn_tdx = TdxLinkButton(self.config)
        self.btn_tdx.message.connect(self.message)
        head.addWidget(self.btn_tdx)
        layout.addLayout(head)

        card = QFrame()
        card.setObjectName("panel")
        grid = QGridLayout(card)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(2)
        self.plan_labels: dict[str, QLabel] = {}
        fields = (
            ("buy", "买入区间"), ("stop", "止损价"), ("target", "2.5R 目标"),
            ("shares", "股数"), ("risk", "风险额"), ("value", "市值占用"),
            ("ma", "MA5/10/20"), ("ratio", "缩量比"), ("pattern", "止跌形态"),
            ("support", "支撑位"), ("pullback_low", "回调5日最低"), ("limitup", "最近涨停"),
        )
        for index, (key, text) in enumerate(fields):
            row, col = divmod(index, 3)
            label = _label(text, "muted")
            value = QLabel("--")
            value.setFont(QFont("Consolas", 10))
            grid.addWidget(label, row * 2, col)
            grid.addWidget(value, row * 2 + 1, col)
            self.plan_labels[key] = value
        for col in range(3):
            grid.setColumnStretch(col, 1)
        layout.addWidget(card)

        lower = QHBoxLayout()
        lower.setSpacing(6)
        lower.addWidget(self._build_checks(), 1)
        lower.addWidget(self._build_scores(), 1)
        layout.addLayout(lower)

        self.chart = CandleChart()
        self.chart.visible_count = 60          # 复核一个回调形态 60 根足够，太密反而看不清
        self.chart.empty_reason = "选中候选后在此复核日K（MA5/10/20 + 支撑/止损/目标）"
        layout.addWidget(self.chart, 1)
        return wrap

    def _build_checks(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("硬性条件（缺一即剔除）", "muted"), 0, 0, 1, 2)
        self.check_labels: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(CHECKS, 1):
            mark = QLabel("—")
            mark.setFixedWidth(14)
            mark.setFont(QFont("Consolas", 9, QFont.Bold))
            label = QLabel(text)
            label.setFont(QFont("Microsoft YaHei UI", 9))
            grid.addWidget(mark, row, 0)
            grid.addWidget(label, row, 1)
            self.check_labels[key] = mark
        return wrap

    def _build_scores(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("评分拆分（满分 100）", "muted"), 0, 0, 1, 2)
        self.score_labels: dict[str, QLabel] = {}
        for row, (key, text, full) in enumerate(SCORE_PARTS, 1):
            grid.addWidget(QLabel(text), row, 0)
            value = QLabel(f"--/{full}")
            value.setFont(QFont("Consolas", 9))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(value, row, 1)
            self.score_labels[key] = value
        self.score_total = QLabel("--")
        self.score_total.setFont(QFont("Consolas", 10, QFont.Bold))
        self.score_total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(_label("合计", "muted"), len(SCORE_PARTS) + 1, 0)
        grid.addWidget(self.score_total, len(SCORE_PARTS) + 1, 1)
        return wrap

    def _build_footer(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.lbl_market = _label("市场环境：--", "muted")
        self.lbl_reject = _label("未通过原因：--", "muted")
        # 一律单行：wordWrap 在固定高度容器里会按一行算高度、再按多行渲染，底部必被切掉半行
        for label in (self.lbl_market, self.lbl_reject):
            label.setWordWrap(False)
            layout.addWidget(label)
        return wrap

    # ================================================================ 扫描
    def start_scan(self) -> None:
        if self.scanner.running:
            return
        self.btn_scan.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.progress.setFormat("准备中…")
        self.lbl_stats.setText("正在读本地日线…（扫描期间可以继续盯盘，结果不影响行情轮询）")
        self.scanner.request_scan()

    def stop(self) -> None:
        self.scanner.stop()
        self._timer.stop()
        if self.result is not None:
            self._persist()

    def _on_tick(self) -> None:
        for event in self.scanner.drain():
            if isinstance(event, ScanProgress):
                self._apply_progress(event)
            elif isinstance(event, ScanDone):
                self._apply_done(event)

    def _apply_progress(self, event: ScanProgress) -> None:
        total = max(event.total, 1)
        percent = int(event.done / total * 100)
        self.progress.setValue(percent)
        self.progress.setFormat(f"{event.phase} {percent}%")

    def _apply_done(self, event: ScanDone) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setVisible(False)
        if event.error:
            self.lbl_stats.setText(f"扫描失败：{event.error}")
            self.message.emit(f"强势回调扫描失败：{event.error}")
            QMessageBox.warning(self, "强势回调扫描", event.error)
            return
        result = event.result
        if result is None:
            return
        self.set_result(result)
        self._persist()
        confirmed = len(result.confirmed)
        watch = len(result.watch)
        self.message.emit(
            f"强势回调扫描完成：评估 {result.scanned} 只，确认 {confirmed}、观察 {watch}，"
            f"耗时 {result.elapsed:.0f}s（数据日 {result.data_date}）")

    def set_result(self, result: ScanResult, restored: bool = False) -> None:
        self.result = result
        self.picks = list(result.picks)
        self._fill_table()
        self.lbl_data.setText(
            f"数据日 {result.data_date or '--'}"
            f" · 距今 {result.stale_days if result.stale_days is not None else '--'} 个自然日"
            f" · {result.sector_mode} 板块强度")
        self.lbl_data.setToolTip("扫描参数：\n" + "\n".join(
            f"{k}：{v}" for k, v in (result.params or {}).items()))
        notes = list(result.notes or [])
        if notes:
            self.badge.setText("日线收盘 · 非实时 ⚠")
            self.badge.setToolTip(BADGE_TIP + "\n\n注意：\n" + "\n".join(notes))
        else:
            self.badge.setText("日线收盘 · 非实时")
            self.badge.setToolTip(BADGE_TIP)
        prefix = "上次扫描结果 · " if restored else ""
        self.lbl_stats.setText(
            f"{prefix}股票池 {result.universe} · 成功评估 {result.scanned} · "
            f"出结论 {result.evaluated} · 确认 {len(result.confirmed)} · 观察 {len(result.watch)} · "
            f"列出 {len(result.picks)} · 耗时 {result.elapsed:.1f}s · 扫描于 {result.scanned_at}")
        self._fill_market(result)
        if restored and not result.frames:
            self.lbl_table_hint.setText("这是上次扫描的结果；选中候选时会重新取该股日K（行情源在线时）")

    def _fill_market(self, result: ScanResult) -> None:
        market = result.market or {}
        if market:
            text = (f"市场环境：样本 {market.get('total', 0)} · "
                    f"涨 {market.get('up', 0)} / 跌 {market.get('down', 0)} / "
                    f"平 {market.get('flat', 0)} · 均 {market.get('avg_pct', 0):+.2f}% · "
                    f"涨停 {market.get('limit_up', 0)} · "
                    f"MA20 上方 {(market.get('above_ratio') or 0) * 100:.1f}%")
            self.lbl_market.setText(text)
            self.lbl_market.setToolTip(text)
        reject = result.reject or {}
        evaluated = max(result.evaluated or len(result.picks), 1)
        short = [f"{SHORT_CHECK[key]} {reject.get(key, 0) / evaluated * 100:.0f}%"
                 for key, _ in CHECKS]
        self.lbl_reject.setText("未通过率（占评估数）：" + " · ".join(short))
        self.lbl_reject.setToolTip("未通过只数与占比：\n" + "\n".join(
            f"{label}：{reject.get(key, 0)} 只（{reject.get(key, 0) / evaluated * 100:.1f}%）"
            for key, label in CHECKS))

    # ================================================================ 候选表
    def _fill_table(self) -> None:
        mode = self.combo.currentData()
        if mode == "buy":
            self._rows = [p for p in self.picks if p.signal == "BUY-A"]
        elif mode == "watch":
            self._rows = [p for p in self.picks if p.signal == "WATCH"]
        else:
            self._rows = list(self.picks)

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._rows))
        for index, pick in enumerate(self._rows):
            self.table.setItem(index, 0, _NumCell(str(index + 1), index + 1, COLORS["text_muted"]))
            self.table.setItem(index, 1, _NumCell(pick.code, pick.code, COLORS["text_dim"],
                                                  align_right=False))
            self.table.setItem(index, 2, _NumCell(pick.name, pick.name, COLORS["text"],
                                                  align_right=False))
            self.table.setItem(index, 3, _NumCell(pick.industry or "--", pick.industry,
                                                  COLORS["text_dim"], align_right=False))
            self.table.setItem(index, 4, _NumCell(str(pick.score), pick.score,
                                                  _score_color(pick.score)))
            self.table.setItem(index, 5, _NumCell(_signal_short(pick.signal), _signal_rank(pick.signal),
                                                  _signal_color(pick.signal), align_right=False))
            self.table.setItem(index, 6, _NumCell(_best_of(pick.close), pick.close, COLORS["text"]))
            self.table.setItem(index, 7, _NumCell(
                _best_of(pick.volume_ratio), pick.volume_ratio,
                COLORS["up"] if (pick.volume_ratio or 9) <= 0.5 else COLORS["text_dim"]))
            self.table.setItem(index, 8, _NumCell(pick.limitup_date or "--",
                                                  pick.limitup_date or "", COLORS["text_dim"],
                                                  align_right=False))
            # 止损距买入价 >8% 的标黄：这是公式算出来的偏宽，不是笔误，得让人一眼看到
            stop_width = (((pick.entry_ref or pick.close or 0) - (pick.stop or 0))
                          / (pick.entry_ref or pick.close or 1) * 100)
            self.table.setItem(index, 9, _NumCell(
                _best_of(pick.stop), pick.stop,
                COLORS["warn"] if stop_width > 8 else COLORS["down"]))
            self.table.setItem(index, 10, _NumCell(_best_of(pick.target), pick.target, COLORS["up"]))
            self.table.item(index, 0).setToolTip(_tooltip(pick))
            self.table.item(index, 9).setToolTip(
                f"止损 {_best_of(pick.stop)} · 距参考买入价 {stop_width:.1f}%"
                + ("（偏宽，见右侧公式）" if stop_width > 8 else ""))
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        if self._rows:
            self.table.selectRow(0)
            self._on_selected()
        else:
            self._show_empty_detail()
        self.lbl_table_hint.setText(
            f"共 {len(self._rows)} 只（全部候选 {len(self.picks)}）· 双击行在单股视图打开")

    def _on_selected(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self._rows):
            return
        symbol = self.table.item(row, 1).text() if self.table.item(row, 1) else ""
        pick = next((p for p in self._rows if p.code == symbol), None)
        if pick is None:
            return
        self._show_detail(pick)

    def _on_activated(self, *_args) -> None:
        if self._current is not None:
            self.symbol_activated.emit(self._current.symbol)

    def _on_open(self) -> None:
        if self._current is not None:
            self.symbol_activated.emit(self._current.symbol)

    def _on_add(self) -> None:
        if self._current is not None:
            self.add_requested.emit([self._current.symbol])

    # ================================================================ 详情
    def _show_detail(self, pick: Pick) -> None:
        self._current = pick
        self.detail_title.setText(f"{pick.name}  {pick.symbol}")
        self.detail_badge.setText(f"{_signal_short(pick.signal)} · {pick.score} 分")
        self.detail_badge.setStyleSheet(f"color:{_signal_color(pick.signal)};")
        self.detail_badge.setToolTip(SIGNAL_LABEL.get(pick.signal, ""))
        self.btn_add.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_tdx.set_symbol(pick.symbol)

        labels = self.plan_labels
        labels["buy"].setText(f"{_best_of(pick.buy_low)} – {_best_of(pick.buy_high)}")
        labels["stop"].setText(_best_of(pick.stop))
        labels["target"].setText(_best_of(pick.target))
        labels["shares"].setText(f"{pick.shares} 股" if pick.shares else "不足 1 手")
        labels["risk"].setText(
            f"{pick.risk_amount:,.0f} 元 / {pick.risk_pct:.2f}%" if pick.shares else "--")
        labels["value"].setText(
            f"{pick.position_value:,.0f} 元 / {pick.value_pct:.2f}%" if pick.shares else "--")
        labels["support"].setText(_best_of(pick.support))
        labels["ratio"].setText(_best_of(pick.volume_ratio))
        labels["pattern"].setText(pick.pattern or "未出现")
        labels["ma"].setText(
            f"{_best_of(pick.ma5)} / {_best_of(pick.ma10)} / {_best_of(pick.ma20)}")
        labels["pullback_low"].setText(_best_of(pick.pullback_low))
        labels["limitup"].setText(
            f"{pick.limitup_date}（{pick.limitup_pct:.0f}%）" if pick.limitup_date else "近10日无")
        stop_width = (((pick.entry_ref or pick.close or 0) - (pick.stop or 0))
                      / (pick.entry_ref or pick.close or 1) * 100)
        if stop_width > 8:
            labels["stop"].setStyleSheet(f"color:{COLORS['warn']};")
            labels["stop"].setToolTip(
                f"止损距参考买入价 {stop_width:.1f}%，偏宽。\n"
                "止损 = min(回调5日低点×0.98, MA20×0.995)，取两条线中更低的一条；\n"
                "止损越宽，1% 风险预算下能买的股数越少。要更紧的止损请改 "
                "pullback.strategy.stop_buffer_pct 后重扫，不要手工改这一行。")
        else:
            labels["stop"].setStyleSheet("")
            labels["stop"].setToolTip(
                f"止损距参考买入价 {stop_width:.1f}%。\n"
                "止损 = min(回调5日低点×0.98, MA20×0.995)")
        labels["buy"].setToolTip(
            "按 MA10 或前期颈线 ±1.5% 给出；目标价与股数按收盘价、止损价计算，\n"
            "若实际在区间上沿成交，每股风险会略大于表中估算。")
        labels["risk"].setToolTip("风险额 = 股数 ×（收盘价 − 止损价），括号内为占账户权益比例")

        for key, mark in self.check_labels.items():
            ok = bool(pick.checks.get(key))
            mark.setText("✓" if ok else "✗")
            mark.setStyleSheet(f"color:{COLORS['down'] if ok else COLORS['up']};")
        for key, text, full in SCORE_PARTS:
            self.score_labels[key].setText(f"{pick.score_parts.get(key, 0)}/{full}")
        self.score_total.setText(f"{pick.score}")
        self.score_total.setStyleSheet(f"color:{_score_color(pick.score)};")

        self._draw_chart(pick)

    def _draw_chart(self, pick: Pick) -> None:
        frame = None
        if self.result is not None:
            frame = (self.result.frames or {}).get(pick.symbol)
        if frame is None or len(frame) < 2:
            # set_bars 会把 empty_reason 重置成默认文案，所以提示文本必须写在它后面
            self.chart.set_bars([], "日K", "C")
            self.chart.set_price_levels([])
            self.chart.empty_reason = "正在取该股日K…（行情源离线时这里会一直是提示）"
            self.chart.update()
            self.bars_needed.emit(pick.symbol)
            return
        from app.db.models import Bar        # 局部导入：只在真正画图时用到

        bars = [
            Bar(symbol=pick.symbol, period="1d", bar_time=row.date.to_pydatetime(),
                open=float(row.open), high=float(row.high), low=float(row.low),
                close=float(row.close), volume=int(row.volume), amount=float(row.amount),
                data_level="C", source="tdx:lday", quality_flag="exchange")
            for row in frame.itertuples(index=False)
        ]
        self.set_chart_bars(pick.symbol, bars)

    def set_chart_bars(self, symbol: str, bars: list) -> None:
        """外部（主窗口）取到日K后回填。只对应当前选中的标的。"""
        if self._current is None or self._current.symbol != symbol or not bars:
            return
        pick = self._current
        self.chart.set_bars(bars, "日K", "C", "exchange")
        self.chart.set_price_levels([
            {"low": pick.buy_low, "high": pick.buy_high,
             "label": f"买入区间 {_best_of(pick.buy_low)}–{_best_of(pick.buy_high)}",
             "color": COLORS["info"]},
            {"price": pick.stop, "label": "止损", "color": COLORS["down"]},
            {"price": pick.target, "label": "2.5R", "color": COLORS["up"]},
        ])

    def _show_empty_detail(self) -> None:
        self._current = None
        self.detail_title.setText("未选择标的")
        self.detail_badge.setText("")
        self.btn_add.setEnabled(False)
        self.btn_open.setEnabled(False)
        self.btn_tdx.set_symbol(None)
        for label in self.plan_labels.values():
            label.setText("--")
        for mark in self.check_labels.values():
            mark.setText("—")
            mark.setStyleSheet(f"color:{COLORS['text_muted']};")
        for key, _text, full in SCORE_PARTS:
            self.score_labels[key].setText(f"--/{full}")
        self.score_total.setText("--")
        self.chart.set_price_levels([])
        self.chart.set_bars([], "日K", "C")
        self.chart.empty_reason = "没有候选：先扫描，或把筛选切回「全部候选」"
        self.chart.update()

    # ================================================================ 落盘/导出
    def _persist(self) -> None:
        if self.result is None:
            return
        try:
            payload = json.dumps(self.result.to_payload(), ensure_ascii=False)
            self.repo.set_setting(SETTING_KEY, payload)
        except Exception as exc:            # 落盘失败不能影响使用
            print(f"[pullback] 保存扫描结果失败: {exc}")

    def _restore(self) -> None:
        raw = self.repo.get_setting(SETTING_KEY, "") or ""
        if not raw:
            return
        try:
            result = ScanResult.from_payload(json.loads(raw))
        except (ValueError, TypeError) as exc:
            print(f"[pullback] 上次扫描结果无法解析，忽略: {exc}")
            return
        if not result.picks:
            return
        self.set_result(result, restored=True)

    def _export(self) -> None:
        if self.result is None:
            QMessageBox.information(self, "导出报告", "还没有扫描结果。")
            return
        outdir = Path((self.config.get("app") or {}).get("data_dir", "./data")) / "reports"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outdir / f"pullback_{stamp}.html"
        try:
            path.write_text(render_report(self.result, self.config), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.message.emit(f"强势回调报告已写入 {path}")
        QMessageBox.information(self, "导出完成", f"已生成单文件报告：\n{path}")


# ---------------------------------------------------------------- 小工具

def _signal_short(signal: str) -> str:
    return {"BUY-A": "确认", "WATCH": "观察"}.get(signal, "剔除")


def _signal_rank(signal: str) -> int:
    return {"BUY-A": 2, "WATCH": 1}.get(signal, 0)


def _signal_color(signal: str) -> str:
    return {"BUY-A": COLORS["up"], "WATCH": COLORS["warn"]}.get(signal, COLORS["text_muted"])


def _score_color(score: int) -> str:
    if score >= 90:
        return COLORS["up"]
    if score >= 80:
        return COLORS["warn"]
    if score >= 70:
        return COLORS["text"]
    return COLORS["text_muted"]


def _tooltip(pick: Pick) -> str:
    checks = "、".join(text for key, text in CHECKS if not pick.checks.get(key)) or "全部通过"
    return (f"{pick.symbol} {pick.name}\n"
            f"评分 {pick.score} · {SIGNAL_LABEL.get(pick.signal, '')}\n"
            f"买入区间 {_best_of(pick.buy_low)}–{_best_of(pick.buy_high)} · "
            f"止损 {_best_of(pick.stop)} · 2.5R {_best_of(pick.target)}\n"
            f"未通过：{checks}\n"
            f"信号日 {pick.date} · 数据为不复权日线收盘价")
