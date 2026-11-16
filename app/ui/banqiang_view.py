"""量学板枪视图：全市场扫描 → 五阶段候选 → 逐项条件/评分拆解/交易计划 → 日K复核。

口径（与 README 铁律一致，别改）：

- 顶栏永远写清 **数据日** 与 **「日线收盘 · C 级」**：板日、枪日、缩量、买点
  都是收盘后才成立的事实，盘中拿不到封单/首封时间/分时人线；
- 每个阶段只展示**已经发生**的事实：没涨停的不进板日，没放量的不进枪日，
  没到触发价的只写「等买点」，绝不把「形态形成中」说成「买入观察」；
- 「回撤 ≥ 1/2」是评级线不是生死线：0.25~0.50 的浅调变体照样入池，但界面必须写明
  并降一档评级，否则 85 分会被人读成「文档全部达标」；
- 入选不足就不凑数，一只没有就明说。
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

from app.services.banqiang import CHECKS, DIM_LABEL, DIM_MAX
from app.services.banqiang_report import render as render_report
from app.services.banqiang_scanner import BanqiangScanner, ScanDone, ScanProgress, ScanResult
from app.ui.charts import CandleChart
from app.ui.panels import _label
from app.ui.tdx_link_button import TdxLinkButton
from app.ui.theme import COLORS

SETTING_KEY = "banqiang.last_scan"

FILTERS = (
    ("all", "全部候选"),
    ("pick", "精选买点"),
    ("买入观察", "买入观察"),
    ("买点临近", "买点临近"),
    ("缩量回调中", "缩量回调中"),
    ("形态形成中", "形态形成中"),
)

BADGE_TIP = (
    "五个阶段都按收盘后的日线判定，不是盘中实时信号。\n"
    "数据来自本地通达信日线（vipdoc/*/lday），价格为不复权。\n"
    "封单额 / 开板次数 / 首封时间 / 分时三波 / 人线：本地日线不提供，一律不推测。\n"
    "筹码是用「成交均价×成交量」估算的，不是真实持仓分布。\n"
    "等级 C：日线收盘口径。")

# 列宽按最长内容给：`买入观察` 要 84、`过枪的高量顶` 要 76，给窄了会被截成「过枪的高…」
COLUMNS = (
    ("#", 30), ("代码", 56), ("名称", 65), ("状态", 76), ("评分", 35), ("评级", 35),
    ("收盘", 52), ("涨幅", 58), ("回撤比", 50), ("买点", 50), ("触发价", 52),
    ("止损", 52), ("行业", 76),
)

STATE_COLOR = {
    "买入观察": "up",
    "买点临近": "warn",
    "缩量回调中": "info",
    "形态形成中": "text_dim",
}


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


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "--"


def _grade_color(grade: str) -> str:
    return {"S": COLORS["up"], "A": COLORS["up"], "B": COLORS["warn"],
            "C": COLORS["text_dim"], "D": COLORS["text_muted"]}.get(grade or "", COLORS["text_muted"])


def _state_color(state: str) -> str:
    return COLORS.get(STATE_COLOR.get(state or "", ""), COLORS["text_muted"])


class BanqiangView(QWidget):
    """主界面第五块：量学板枪。数据来自本地通达信日线，不占 tqcenter 会话。"""

    symbol_activated = Signal(str)        # 双击行 → 主界面切到该股
    add_requested = Signal(list)          # 加入「量学板枪池」分组
    bars_needed = Signal(str)             # 需要该股日K
    message = Signal(str)                 # 写进右下角日志

    def __init__(self, config: dict, repo, parent=None) -> None:
        super().__init__(parent)
        self.config = config or {}
        self.repo = repo
        self.result: ScanResult | None = None
        self._rows: list[dict] = []
        self._current: dict | None = None
        self.scanner = BanqiangScanner(config, repo)

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
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([720, 520])
        layout.addWidget(split, 1)
        layout.addWidget(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        title = QLabel("量学板枪")
        title.setProperty("role", "title")
        bar.addWidget(title)

        self.badge = _label("日线收盘 · C 级", "warn")
        self.badge.setToolTip(BADGE_TIP)
        bar.addWidget(self.badge)

        self.lbl_data = _label("数据日 --", "muted")
        bar.addWidget(self.lbl_data)

        bar.addWidget(_label("状态", "muted"))
        self.combo = QComboBox()
        for key, text in FILTERS:
            self.combo.addItem(text, key)
        self.combo.currentIndexChanged.connect(lambda _: self._fill_table())
        bar.addWidget(self.combo)
        bar.addStretch(1)

        self.btn_scan = QPushButton("扫描全市场")
        self.btn_scan.setProperty("accent", True)
        self.btn_scan.setToolTip("读本地日线跑五阶段（约 20~40 秒）；扫描期间可以继续盯盘")
        self.btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_scan)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.scanner.request_stop)
        bar.addWidget(self.btn_stop)

        self.btn_export = QPushButton("导出报告")
        self.btn_export.setToolTip("导出单文件深色复盘页（口径 / 漏斗 / 分组 / 买点 / 结构 / 风险）")
        self.btn_export.clicked.connect(self._export)
        bar.addWidget(self.btn_export)
        return bar

    def _build_stats(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.lbl_stats = _label("尚未扫描。点右上角「扫描全市场」，用本地日线跑一遍板枪五阶段。", "muted")
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
        self.table.setMinimumWidth(600)
        head = self.table.horizontalHeader()
        for col, (_, width) in enumerate(COLUMNS):
            head.setSectionResizeMode(col, QHeaderView.Fixed)
            self.table.setColumnWidth(col, width)
        head.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
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
        head.setContentsMargins(0, 0, 6, 0)
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
            ("state", "状态"), ("board_gun", "板日 / 枪日"), ("score", "评分"),
            ("ratio", "回撤比"), ("pull_days", "回调天数"), ("close", "收盘 / 涨幅"),
            ("buy_type", "买点"), ("trigger", "触发价"), ("stop", "止损"),
            ("t1", "目标1"), ("t2", "目标2"), ("t3", "目标3"),
        )
        for index, (key, text) in enumerate(fields):
            row, col = divmod(index, 3)
            grid.addWidget(_label(text, "muted"), row * 2, col)
            value = QLabel("--")
            value.setFont(QFont("Consolas", 10))
            grid.addWidget(value, row * 2 + 1, col)
            self.plan_labels[key] = value
        for col in range(3):
            grid.setColumnStretch(col, 1)

        # 买点条件 / 风控三行占满整宽，否则会被卡片右边框切掉半句
        extra_row = (len(fields) // 3) * 2
        for offset, (key, text) in enumerate((("cond", "买点条件"), ("risk", "结构底线 / 参考买价 / 盈亏比"),
                                              ("invalidate", "失效条件"))):
            grid.addWidget(_label(text, "muted"), extra_row + offset * 2, 0, 1, 3)
            value = QLabel("--")
            value.setFont(QFont("Microsoft YaHei UI", 9))
            grid.addWidget(value, extra_row + offset * 2 + 1, 0, 1, 3)
            self.plan_labels[key] = value
        layout.addWidget(card)

        lower = QHBoxLayout()
        lower.setSpacing(6)
        lower.addWidget(self._build_checks(), 1)
        lower.addWidget(self._build_scores(), 1)
        layout.addLayout(lower)

        self.chart = CandleChart()
        self.chart.visible_count = 60
        self.chart.empty_reason = "选中标的后在此复核日K（买点触发价 / 止损 / 目标价位线）"
        layout.addWidget(self.chart, 1)
        return wrap

    def _build_checks(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("五阶段条件（逐项核）", "muted"), 0, 0, 1, 2)
        self.check_labels: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(CHECKS.items(), 1):
            mark = QLabel("—")
            mark.setFixedWidth(14)
            mark.setFont(QFont("Consolas", 9, QFont.Bold))
            label = QLabel(text)
            label.setFont(QFont("Microsoft YaHei UI", 8))
            grid.addWidget(mark, row, 0)
            grid.addWidget(label, row, 1)
            self.check_labels[key] = mark
        self.lbl_extra = _label("阶段进度：--", "muted")
        self.lbl_extra.setFont(QFont("Microsoft YaHei UI", 8))
        grid.addWidget(self.lbl_extra, len(CHECKS) + 1, 0, 1, 2)
        return wrap

    def _build_scores(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("评分拆解（满分 100）", "muted"), 0, 0, 1, 2)
        self.score_labels: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(DIM_LABEL.items(), 1):
            label = QLabel(text)
            label.setFont(QFont("Microsoft YaHei UI", 8))
            grid.addWidget(label, row, 0)
            value = QLabel(f"--/{DIM_MAX[key]}")
            value.setFont(QFont("Consolas", 9))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(value, row, 1)
            self.score_labels[key] = value
        self.score_total = QLabel("--")
        self.score_total.setFont(QFont("Consolas", 10, QFont.Bold))
        self.score_total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(_label("总分 / 评级", "muted"), len(DIM_LABEL) + 1, 0)
        grid.addWidget(self.score_total, len(DIM_LABEL) + 1, 1)
        self.lbl_gate = _label("扣分说明：--", "muted")
        self.lbl_gate.setFont(QFont("Microsoft YaHei UI", 8))
        self.lbl_gate.setWordWrap(True)
        grid.addWidget(self.lbl_gate, len(DIM_LABEL) + 2, 0, 1, 2)
        return wrap

    def _build_footer(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.lbl_funnel = _label("五阶段漏斗：--", "muted")
        self.lbl_params = _label("扫描参数：--", "muted")
        for label in (self.lbl_funnel, self.lbl_params):
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
        self.lbl_stats.setText("正在读本地日线…（扫描期间可以继续盯盘，不影响行情轮询）")
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
        percent = int(event.done / total * 100) if event.total else 0
        self.progress.setValue(percent)
        self.progress.setFormat(f"{event.phase} {percent}%")

    def _apply_done(self, event: ScanDone) -> None:
        self.btn_scan.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.progress.setVisible(False)
        if event.error:
            self.lbl_stats.setText(f"扫描失败：{event.error.splitlines()[0]}")
            self.lbl_stats.setToolTip(event.error)
            self.message.emit(f"量学板枪扫描失败：{event.error.splitlines()[0]}")
            QMessageBox.warning(self, "量学板枪", event.error)
            return
        result = event.result
        if result is None:
            return
        self.set_result(result)
        self._persist()
        counts = result.counts
        self.message.emit(
            f"量学板枪扫描完成：评估 {result.scanned} 只，买入观察 {counts['买入观察']}、"
            f"买点临近 {counts['买点临近']}、缩量回调 {counts['缩量回调中']}、"
            f"形态形成 {counts['形态形成中']}，耗时 {result.elapsed:.0f}s（数据日 {result.data_date}）")

    # ================================================================ 结果
    def set_result(self, result: ScanResult, restored: bool = False) -> None:
        self.result = result
        self._fill_table()
        self.lbl_data.setText(
            f"数据日 {result.data_date or '--'} · 滞后 "
            f"{result.stale_days if result.stale_days is not None else '--'} 天")
        self.lbl_data.setToolTip("扫描参数：\n" + "\n".join(
            f"{k}：{v}" for k, v in (result.params or {}).items()))
        notes = list(result.notes or [])
        self.badge.setText("日线收盘 · C 级" + (" ⚠" if notes else ""))
        self.badge.setToolTip(BADGE_TIP + ("\n\n注意：\n" + "\n".join(notes) if notes else ""))
        counts = result.counts
        prefix = "上次扫描结果 · " if restored else ""
        self.lbl_stats.setText(
            f"{prefix}股票池 {result.universe} · 有效快照 {result.scanned} · 跳过 {result.errors} · "
            f"买入观察 {counts['买入观察']} / 买点临近 {counts['买点临近']} / "
            f"缩量回调 {counts['缩量回调中']} / 形态形成 {counts['形态形成中']} · "
            f"耗时 {result.elapsed:.1f}s · 扫描于 {result.scanned_at}")
        self.lbl_params.setText(
            "扫描参数：" + " · ".join(f"{k} {v}" for k, v in (result.params or {}).items()))
        funnel = result.funnel or {}
        top = list(funnel.items())[:4]
        self.lbl_funnel.setText(
            "五阶段漏斗：" + (" · ".join(f"{name} {count}" for name, count in top)
                            if top else "--（本次没有筛除记录）"))
        self.lbl_funnel.setToolTip(
            "筛除原因统计（推进到哪一阶段倒下）：\n"
            + ("\n".join(f"{name}：{count} 只" for name, count in funnel.items()) or "--")
            + "\n\n「板日-最近 5 日无涨停」占大头是正常的：板日本就稀少。")
        if restored and not result.frames:
            self.lbl_table_hint.setText("这是上次扫描的结果；选中标的时会重新取该股日K（行情源在线时）")

    # ---------------------------------------------------------------- 表格
    def _collect_rows(self) -> list[dict]:
        mode = self.combo.currentData()
        result = self.result
        if result is None:
            return []
        if mode == "pick":
            source = list(result.picks)
        elif mode == "all":
            source = result.all_candidates
        else:
            source = list(result.groups.get(mode) or [])
        rows = []
        for item in source:
            score = item.get("score") or {}
            plan = item.get("plan") or {}
            s5 = item.get("s5") or {}
            s3 = item.get("s3") or {}
            rows.append({
                "symbol": item.get("symbol"),
                "code": item.get("code"),
                "name": item.get("name"),
                "industry": item.get("industry") or "",
                "state": item.get("state") or "",
                "score": score.get("total"),
                "grade": score.get("grade") or "",
                "close": item.get("close"),
                "pct": item.get("pct"),
                "ratio": s3.get("ratio"),
                "buy_type": s5.get("buy_type") or "",
                "trigger": s5.get("buy_trigger_price"),
                "stop": plan.get("stop"),
                "item": item,
            })
        return rows

    def _fill_table(self) -> None:
        self._rows = self._collect_rows()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self.table.setItem(index, 0, _NumCell(str(index + 1), index + 1, COLORS["text_muted"]))
            self.table.setItem(index, 1, _NumCell(row["code"], row["code"],
                                                 COLORS["text_dim"], align_right=False))
            self.table.setItem(index, 2, _NumCell(row["name"], row["name"],
                                                 COLORS["text"], align_right=False))
            self.table.setItem(index, 3, _NumCell(row["state"], row["state"],
                                                 _state_color(row["state"]), align_right=False))
            self.table.setItem(index, 4, _NumCell(_num(row["score"], 0), row["score"] or 0,
                                                 _grade_color(row["grade"])))
            self.table.setItem(index, 5, _NumCell(row["grade"] or "--",
                                                 {"S": 5, "A": 4, "B": 3, "C": 2}.get(
                                                     row["grade"] or "", 0),
                                                 _grade_color(row["grade"]),
                                                 align_right=False))
            self.table.setItem(index, 6, _NumCell(_num(row["close"]), row["close"], COLORS["text"]))
            pct = row["pct"]
            self.table.setItem(index, 7, _NumCell(
                "--" if pct is None else f"{pct:+.2f}%", pct if pct is not None else 0,
                COLORS["up"] if (pct or 0) > 0 else COLORS["down"] if (pct or 0) < 0
                else COLORS["text_dim"]))
            self.table.setItem(index, 8, _NumCell(_num(row["ratio"]), row["ratio"] or 0,
                                                 COLORS["warn"] if (row["ratio"] or 0) < 0.5
                                                 else COLORS["text_dim"]))
            self.table.setItem(index, 9, _NumCell(row["buy_type"] or "--", row["buy_type"] or "",
                                                 COLORS["text_dim"], align_right=False))
            self.table.setItem(index, 10, _NumCell(_num(row["trigger"]), row["trigger"] or 0,
                                                  COLORS["info"]))
            self.table.setItem(index, 11, _NumCell(_num(row["stop"]), row["stop"] or 0,
                                                  COLORS["down"]))
            self.table.setItem(index, 12, _NumCell(row["industry"] or "--", row["industry"] or "",
                                                  COLORS["text_dim"], align_right=False))
            self.table.item(index, 0).setToolTip(self._tooltip(row))
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        if self._rows:
            self.table.selectRow(0)
            self._on_selected()
        else:
            self._show_empty_detail()
            if self.result is not None:
                # 默认筛选是「精选买点」；一只都没有时必须说清结构还在哪儿，别让人以为扫描白跑
                counts = self.result.counts
                self.lbl_table_hint.setText(
                    f"当前筛选 0 只 · 缩量回调中 {counts['缩量回调中']} · 买点临近 "
                    f"{counts['买点临近']} · 买入观察 {counts['买入观察']} · 形态形成中 "
                    f"{counts['形态形成中']}（把「状态」切到「全部候选」可看全部结构）")
                return
        self.lbl_table_hint.setText(
            f"共 {len(self._rows)} 只 · 双击行在单股视图打开 · 状态/评分按日线收盘口径")

    @staticmethod
    def _tooltip(row: dict) -> str:
        item = row.get("item") or {}
        lines = [f"{row['name']}（{row['symbol']}）", f"状态：{row['state'] or '--'}"]
        lines.append(item.get("reason") or "")
        plan = item.get("plan") or {}
        lines.append(f"触发 {_num(row['trigger'])} · 止损 {_num(plan.get('stop'))} · "
                     f"目标 {_num(plan.get('target1'))}/{_num(plan.get('target2'))}/"
                     f"{_num(plan.get('target3'))}")
        checks = item.get("checks") or {}
        failed = [CHECKS[k] for k, v in checks.items() if k in CHECKS and not v.get("ok")]
        if failed:
            lines.append("未满足：" + "；".join(failed))
        return "\n".join(x for x in lines if x)

    def _on_selected(self) -> None:
        table_row = self.table.currentRow()
        if not 0 <= table_row < len(self._rows):
            return
        # 排序后行号会变，必须按「#」单元格里保存的名次回查，不能直接用行号索引
        cell = self.table.item(table_row, 0)
        rank = cell.data(Qt.UserRole) if cell is not None else None
        if not isinstance(rank, int) or not 1 <= rank <= len(self._rows):
            return
        self._show_detail(self._rows[rank - 1])

    def _on_activated(self, *_args) -> None:
        if self._current is not None:
            self.symbol_activated.emit(self._current["symbol"])

    def _on_open(self) -> None:
        if self._current is not None:
            self.symbol_activated.emit(self._current["symbol"])

    def _on_add(self) -> None:
        if self._current is not None:
            self.add_requested.emit([self._current["symbol"]])

    # ---------------------------------------------------------------- 详情
    def _show_detail(self, row: dict) -> None:
        self._current = row
        item = row["item"]
        score = item.get("score") or {}
        plan = item.get("plan") or {}
        s1, s2, s3, s4, s5 = (item.get("s1"), item.get("s2"), item.get("s3"),
                              item.get("s4"), item.get("s5"))
        self.detail_title.setText(f"{row['name']}  {row['symbol']}")
        badge = f"{row['state']}"
        if score.get("total") is not None:
            badge += f" · {_num(score.get('total'), 0)} 分 {score.get('grade') or ''}"
        if item.get("early"):
            badge += " · 枪日未出"
        self.detail_badge.setText(badge)
        self.detail_badge.setStyleSheet(f"color:{_state_color(row['state'])};")
        self.btn_add.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_tdx.set_symbol(row["symbol"])

        labels = self.plan_labels
        board = item.get("board_date") or "--"
        gun = item.get("gun_date") or "未出"
        labels["board_gun"].setText(f"{board} / {gun}")
        labels["score"].setText(
            "--" if score.get("total") is None else f"{_num(score.get('total'), 0)} / {score.get('grade') or '--'}")
        labels["ratio"].setText("--" if not s3 else f"{_num(s3.get('ratio'))}"
                                                    + ("（达标）" if s3.get("ratio", 0) >= 0.5 else "（浅调）"))
        labels["pull_days"].setText("--" if not s3 else f"{s3.get('pull_days')} 日")
        labels["close"].setText(f"{_num(item.get('close'))} / {_num(item.get('pct'))}%")
        labels["buy_type"].setText((s5 or {}).get("buy_type") or ("--（枪日未出）" if item.get("early") else "--"))
        labels["trigger"].setText(_num((s5 or {}).get("buy_trigger_price")))
        labels["stop"].setText(_num(plan.get("stop")))
        labels["t1"].setText(_num(plan.get("target1")))
        labels["t2"].setText(_num(plan.get("target2")))
        labels["t3"].setText(_num(plan.get("target3")))
        labels["cond"].setText((s5 or {}).get("buy_cond") or "--")
        # 控制位一行里塞四个数，太长的解释放 tooltip，别让卡片右边框切掉半句
        rr_text = (f"结构底线 {_num(plan.get('deep_floor'))} · 参考买价 {_num(plan.get('buy'))}"
                   f" · 盈亏比 1:{_num(plan.get('rr'))}")
        if plan and not plan.get("target1_valid", True):
            rr_text += "（目标1 已过）"
        labels["risk"].setText(rr_text if plan else "--")
        labels["risk"].setToolTip(
            (f"结构底线 = 板日开盘 {_num(plan.get('deep_floor'))}（跌破说明整段结构已废）\n"
             f"参考买价 {_num(plan.get('buy'))} · 止损 {_num(plan.get('stop'))}\n"
             f"盈亏比 =（目标1 − 买价）/（买价 − 止损）= 1:{_num(plan.get('rr'))}"
             + ("\n目标1 已低于现价：板枪顶部被当日阳线越过，按目标2/3 看空间"
                if not plan.get("target1_valid", True) else "")) if plan else "")
        labels["invalidate"].setText(plan.get("invalidate") or "--")
        labels["state"].setText(row["state"] or "--")
        labels["state"].setStyleSheet(f"color:{_state_color(row['state'])};")
        for key in ("board_gun", "score", "ratio", "pull_days", "close", "buy_type",
                    "trigger", "stop", "t1", "t2", "t3", "cond", "invalidate"):
            labels[key].setToolTip(labels[key].text())

        checks = item.get("checks") or {}
        for key, mark in self.check_labels.items():
            entry = checks.get(key)
            if entry is None:
                mark.setText("—")
                mark.setStyleSheet(f"color:{COLORS['text_muted']};")
                mark.setToolTip("枪日未出，该项尚未发生")
                continue
            ok = bool(entry.get("ok"))
            mark.setText("✓" if ok else "✗")
            mark.setStyleSheet(f"color:{COLORS['down'] if ok else COLORS['up']};")
            mark.setToolTip(entry.get("value") or "")
        if item.get("early"):
            self.lbl_extra.setText("阶段进度：板日已成立，枪日未出（形态形成中）")
        else:
            self.lbl_extra.setText(
                f"阶段进度：板日 ✓ → 枪日 ✓（{s2.get('gun_type')} {s2.get('gun_grade')} 级）"
                f" → 回调 {s3.get('pull_days')} 日 → {row['state']}")

        dims = score.get("dims") or {}
        for key, value in self.score_labels.items():
            dim = dims.get(key) or {}
            value.setText(f"{_num(dim.get('score'), 1)}/{DIM_MAX[key]}")
            items = dim.get("items") or []
            value.setToolTip("\n".join(
                f"· {x.get('name')}：{x.get('data')} → {x.get('points')} 分"
                + (f"（{x.get('note')}）" if x.get("note") else "")
                for x in items) or "枪日未出，尚未评分")
        self.score_total.setText(
            "--" if score.get("total") is None else f"{score.get('total')} {score.get('grade') or ''}")
        self.score_total.setStyleSheet(f"color:{_grade_color(score.get('grade') or '')};")
        notes = list(score.get("notes") or [])
        veto = list(score.get("veto") or [])
        if veto:
            notes.insert(0, "一票否决：" + "；".join(veto))
        self.lbl_gate.setText("扣分说明：" + ("；".join(notes) if notes else "无（各维度按实测值给分）"))
        self.lbl_gate.setToolTip("\n".join(n for n in (score.get("notes") or []) + veto) or "")
        self._draw_chart(row["symbol"], plan, item)

    def _draw_chart(self, symbol: str, plan: dict | None, item: dict | None) -> None:
        frame = None
        if self.result is not None:
            frame = (self.result.frames or {}).get(symbol)
        if not frame or len(frame) < 2:
            self.chart.set_bars([], "日K", "C")
            self.chart.set_price_levels([])
            self.chart.empty_reason = "正在取该股日K…（行情源离线时这里会一直是提示）"
            self.chart.update()
            self.bars_needed.emit(symbol)
            return
        from app.db.models import Bar

        bars = [
            Bar(symbol=symbol, period="1d",
                bar_time=datetime.strptime(str(row["date"]), "%Y%m%d"),
                open=float(row["open"]), high=float(row["high"]), low=float(row["low"]),
                close=float(row["close"]), volume=int(row["volume"]),
                amount=float(row["amount"]), data_level="C", source="tdx:lday",
                quality_flag="exchange")
            for row in frame
        ]
        self.set_chart_bars(symbol, bars)

    def set_chart_bars(self, symbol: str, bars: list) -> None:
        """主窗口取到日K后回填。只对应当前选中标的。"""
        if self._current is None or self._current["symbol"] != symbol or not bars:
            return
        self.chart.set_bars(bars, "日K", "C", "exchange")
        item = self._current.get("item") or {}
        plan = item.get("plan") or {}
        s5 = item.get("s5") or {}
        levels = []
        if s5.get("buy_trigger_price"):
            levels.append({"price": s5["buy_trigger_price"], "label": "买点",
                           "color": COLORS["info"]})
        if plan.get("stop"):
            levels.append({"price": plan["stop"], "label": "止损", "color": COLORS["down"]})
        if plan.get("target1"):
            levels.append({"price": plan["target1"], "label": "目标1", "color": COLORS["up"]})
        if plan.get("target2") and plan.get("target2") != plan.get("target1"):
            levels.append({"price": plan["target2"], "label": "目标2", "color": COLORS["up"]})
        self.chart.set_price_levels(levels)

    def _show_empty_detail(self) -> None:
        self._current = None
        self.detail_title.setText("未选择标的")
        self.detail_badge.setText("")
        self.btn_add.setEnabled(False)
        self.btn_open.setEnabled(False)
        self.btn_tdx.set_symbol(None)
        for key, label in self.plan_labels.items():
            label.setText("--")
            label.setToolTip("")
            if key == "state":
                label.setStyleSheet("")
        for mark in self.check_labels.values():
            mark.setText("—")
            mark.setStyleSheet(f"color:{COLORS['text_muted']};")
            mark.setToolTip("")
        for key, value in self.score_labels.items():
            value.setText(f"--/{DIM_MAX[key]}")
            value.setToolTip("")
        self.score_total.setText("--")
        self.score_total.setStyleSheet("")
        self.lbl_gate.setText("扣分说明：--")
        self.lbl_extra.setText("阶段进度：--")
        self.chart.set_price_levels([])
        self.chart.set_bars([], "日K", "C")
        self.chart.empty_reason = "没有标的：先扫描，或把状态切回「全部候选」"
        self.chart.update()

    # ---------------------------------------------------------------- 落盘/导出
    def _persist(self) -> None:
        if self.result is None:
            return
        try:
            self.repo.set_setting(SETTING_KEY,
                                  json.dumps(self.result.to_payload(), ensure_ascii=False))
        except Exception as exc:            # noqa: BLE001 - 落盘失败不能影响使用
            print(f"[banqiang] 保存扫描结果失败: {exc}")

    def _restore(self) -> None:
        raw = self.repo.get_setting(SETTING_KEY, "") or ""
        if not raw:
            return
        try:
            result = ScanResult.from_payload(json.loads(raw))
        except (ValueError, TypeError) as exc:
            print(f"[banqiang] 上次扫描结果无法解析，忽略: {exc}")
            return
        if not result.data_date:
            return
        self.set_result(result, restored=True)

    def _export(self) -> None:
        if self.result is None:
            QMessageBox.information(self, "导出报告", "还没有扫描结果。")
            return
        outdir = Path((self.config.get("app") or {}).get("data_dir", "./data")) / "reports"
        outdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = outdir / f"banqiang_{stamp}.html"
        try:
            path.write_text(render_report(self.result, self.config), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.message.emit(f"量学板枪报告已写入 {path}")
        QMessageBox.information(self, "导出完成", f"已生成单文件报告：\n{path}")
