"""涨停四阶段视图：全市场扫描 → 四阶段候选 → 评分拆解与交易计划 → 日K复核。

口径（与 README 铁律一致，别改）：

- 顶栏永远写清 **数据日** 与 **「日线收盘 · C 级」**：四个阶段都是收盘后成立的事实，
  盘中拿不到封单/首封时间/炸板次数，也就不可能盘中说"三倍量确认"；
- 每个阶段只展示**已经发生**的事实：没涨停的不进首板，没放量的不进三倍量，
  没突破的只写"等待启动"，绝不把 S1 说成 S2；
- 评分按阶段门控（S1/S2 上限 55 权重、S3 75、S4 100），界面必须同时给
  「已验证 / 未验证 / 数据完整度」，不然 90 分会被读成全阶段满分；
- 入选不足 10 只就少给，一只没有就明说，不降标准凑数。
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

from app.services.limitup import (
    DIM_LABEL,
    LIMITUP_CHECKS,
    STAGE_LABEL,
    STATE_LABEL,
    WEIGHTS,
)
from app.services.limitup_report import render as render_report
from app.services.limitup_scanner import LimitupScanner, ScanDone, ScanProgress, ScanResult
from app.ui.charts import CandleChart
from app.ui.panels import _label
from app.ui.tdx_link_button import TdxLinkButton
from app.ui.theme import COLORS

SETTING_KEY = "limitup.last_scan"

FILTERS = (
    ("pick", "最终精选"),
    ("all", "全部候选"),
    ("S1", "首板发现"),
    ("S2", "三倍量确认"),
    ("S3", "次日承接"),
    ("S4", "二次启动"),
    ("pool", "股票池"),
)

BADGE_TIP = (
    "四个阶段都按收盘后的日线判定，不是盘中实时信号。\n"
    "数据来自本地通达信日线（vipdoc/*/lday），价格为不复权。\n"
    "首封时间 / 炸板次数 / 封单金额 / 分时均价本地日线不提供，一律不推测。\n"
    "等级 C：日线收盘口径。")

# 列宽按最长内容给：`+10.10%` 要 64、`三倍量确认` 要 72、`10.04` 要 52，
# 给窄了 Qt 会截成「+9...」「三倍量…」，看起来像数据缺失
COLUMNS = (
    ("#", 30), ("代码", 60), ("名称", 70), ("阶段", 84), ("评分", 44), ("评级", 44),
    ("收盘", 58), ("涨幅", 64), ("5日量能", 64), ("换手", 54), ("行业", 80),
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


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "--"


def _grade_color(grade: str) -> str:
    return {"S级": COLORS["up"], "A级": COLORS["up"], "B级": COLORS["warn"],
            "C级": COLORS["text_dim"]}.get(grade or "", COLORS["text_muted"])


def _stage_color(stage: str) -> str:
    return {"S4": COLORS["up"], "S3": COLORS["info"], "S2": COLORS["warn"],
            "S1": COLORS["text_dim"]}.get(stage or "", COLORS["text_muted"])


class LimitupView(QWidget):
    """主界面第四块：涨停四阶段。数据来自本地通达信日线，不占 tqcenter 会话。"""

    symbol_activated = Signal(str)        # 双击行 → 主界面切到该股
    add_requested = Signal(list)          # 加入「涨停四阶段池」分组
    bars_needed = Signal(str)             # 需要该股日K
    message = Signal(str)                 # 写进右下角日志

    def __init__(self, config: dict, repo, parent=None) -> None:
        super().__init__(parent)
        self.config = config or {}
        self.repo = repo
        self.result: ScanResult | None = None
        self._rows: list[dict] = []
        self._current: dict | None = None
        self.scanner = LimitupScanner(config, repo)

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
        split.setSizes([700, 520])
        layout.addWidget(split, 1)
        layout.addWidget(self._build_footer())

    def _build_header(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(8)
        title = QLabel("涨停四阶段")
        title.setProperty("role", "title")
        bar.addWidget(title)

        self.badge = _label("日线收盘 · C 级", "warn")
        self.badge.setToolTip(BADGE_TIP)
        bar.addWidget(self.badge)

        self.lbl_data = _label("数据日 --", "muted")
        bar.addWidget(self.lbl_data)

        bar.addWidget(_label("阶段", "muted"))
        self.combo = QComboBox()
        for key, text in FILTERS:
            self.combo.addItem(text, key)
        self.combo.currentIndexChanged.connect(lambda _: self._fill_table())
        bar.addWidget(self.combo)
        bar.addStretch(1)

        self.btn_scan = QPushButton("扫描全市场")
        self.btn_scan.setProperty("accent", True)
        self.btn_scan.setToolTip("读本地日线跑四阶段（约 20 秒）；扫描期间可以继续盯盘")
        self.btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_scan)

        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.scanner.request_stop)
        bar.addWidget(self.btn_stop)

        self.btn_export = QPushButton("导出报告")
        self.btn_export.setToolTip("导出单文件深色复盘页（市场环境 / 四阶段 / 精选 / 计划 / 风险 / 池）")
        self.btn_export.clicked.connect(self._export)
        bar.addWidget(self.btn_export)
        return bar

    def _build_stats(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)
        self.lbl_stats = _label("尚未扫描。点右上角「扫描全市场」，用本地日线跑一遍四阶段。", "muted")
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
        head.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.Stretch)
        # 行序就是策略排名；把排序指示器固定在「#」升序，否则 Qt 会沿用上次的降序
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
            ("stage", "阶段"), ("board", "首板日"), ("score", "评分"),
            ("support", "支撑"), ("stop", "止损"), ("target", "目标1 / 2"),
            ("close", "收盘"), ("vol", "5日量能"), ("turnover", "换手"),
            ("risk", "风险"), ("complete", "数据完整度"), ("plan", "失效条件"),
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
        # 触发条件太长，单独占一整行，否则会被卡片右边框切掉半句
        trigger_row = (len(fields) // 3) * 2
        grid.addWidget(_label("触发条件", "muted"), trigger_row, 0, 1, 3)
        self.plan_labels["trigger"] = QLabel("--")
        self.plan_labels["trigger"].setFont(QFont("Microsoft YaHei UI", 9))
        grid.addWidget(self.plan_labels["trigger"], trigger_row + 1, 0, 1, 3)
        layout.addWidget(card)

        lower = QHBoxLayout()
        lower.setSpacing(6)
        lower.addWidget(self._build_checks(), 1)
        lower.addWidget(self._build_scores(), 1)
        layout.addLayout(lower)

        self.chart = CandleChart()
        self.chart.visible_count = 60
        self.chart.empty_reason = "选中标的后在此复核日K（支撑 / 止损 / 目标价位线）"
        layout.addWidget(self.chart, 1)
        return wrap

    def _build_checks(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("阶段条件（逐项核）", "muted"), 0, 0, 1, 2)
        self.check_labels: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(LIMITUP_CHECKS.items(), 1):
            mark = QLabel("—")
            mark.setFixedWidth(14)
            mark.setFont(QFont("Consolas", 9, QFont.Bold))
            label = QLabel(text)
            label.setFont(QFont("Microsoft YaHei UI", 9))
            grid.addWidget(mark, row, 0)
            grid.addWidget(label, row, 1)
            self.check_labels[key] = mark
        self.lbl_extra = _label("承接 / 启动：--", "muted")
        grid.addWidget(self.lbl_extra, len(LIMITUP_CHECKS) + 1, 0, 1, 2)
        return wrap

    def _build_scores(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("panel")
        grid = QGridLayout(wrap)
        grid.setContentsMargins(8, 6, 8, 6)
        grid.setVerticalSpacing(1)
        grid.addWidget(_label("评分拆解（按阶段门控）", "muted"), 0, 0, 1, 2)
        self.score_labels: dict[str, QLabel] = {}
        for row, (key, text) in enumerate(DIM_LABEL.items(), 1):
            grid.addWidget(QLabel(text), row, 0)
            value = QLabel(f"--/{WEIGHTS[key]:.0f}")
            value.setFont(QFont("Consolas", 9))
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(value, row, 1)
            self.score_labels[key] = value
        self.score_total = QLabel("--")
        self.score_total.setFont(QFont("Consolas", 10, QFont.Bold))
        self.score_total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        grid.addWidget(_label("归一化", "muted"), len(DIM_LABEL) + 1, 0)
        grid.addWidget(self.score_total, len(DIM_LABEL) + 1, 1)
        self.lbl_gate = _label("门控：--", "muted")
        grid.addWidget(self.lbl_gate, len(DIM_LABEL) + 2, 0, 1, 2)
        return wrap

    def _build_footer(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.lbl_market = _label("市场环境：--", "muted")
        self.lbl_pool = _label("股票池：--", "muted")
        for label in (self.lbl_market, self.lbl_pool):
            label.setWordWrap(False)          # wordWrap 在固定高度里会被切掉半行
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
            self.message.emit(f"涨停四阶段扫描失败：{event.error.splitlines()[0]}")
            QMessageBox.warning(self, "涨停四阶段", event.error)
            return
        result = event.result
        if result is None:
            return
        self.set_result(result)
        self._persist()
        counts = result.counts
        self.message.emit(
            f"涨停四阶段扫描完成：评估 {result.scanned} 只，首板 {counts['S1']}、"
            f"三倍量 {counts['S2']}、承接 {counts['S3']}、启动 {counts['S4']}，"
            f"精选 {len(result.picks)}，耗时 {result.elapsed:.0f}s（数据日 {result.data_date}）")

    # ================================================================ 结果
    def set_result(self, result: ScanResult, restored: bool = False) -> None:
        self.result = result
        self._fill_table()
        self.lbl_data.setText(
            f"数据日 {result.data_date or '--'} · 滞后 {result.stale_days if result.stale_days is not None else '--'} 天")
        self.lbl_data.setToolTip("扫描参数：\n" + "\n".join(
            f"{k}：{v}" for k, v in (result.params or {}).items()))
        notes = list(result.notes or [])
        self.badge.setText("日线收盘 · C 级" + (" ⚠" if notes else ""))
        self.badge.setToolTip(BADGE_TIP + ("\n\n注意：\n" + "\n".join(notes) if notes else ""))
        counts = result.counts
        prefix = "上次扫描结果 · " if restored else ""
        self.lbl_stats.setText(
            f"{prefix}股票池 {result.universe} · 有效快照 {result.scanned} · 跳过 {result.errors} · "
            f"首板 {counts['S1']} / 三倍量 {counts['S2']} / 承接 {counts['S3']} / 启动 {counts['S4']} · "
            f"精选 {len(result.picks)} · 耗时 {result.elapsed:.1f}s · 扫描于 {result.scanned_at}")
        market = result.market or {}
        self.lbl_market.setText(
            f"市场环境：涨停 {market.get('limit_up_count', 0)}（首板 {market.get('first_board_count', 0)}）· "
            f"触板未封 {market.get('touched_count', 0)} · 炸板率 {_num(market.get('fail_rate'), 1)}% · "
            f"情绪 {market.get('mood') or '--'}（{_num(market.get('sentiment_score'), 1)}）· "
            f"主线 {'、'.join(market.get('main_lines') or []) or '--'}")
        self.lbl_market.setToolTip(market.get("sector_caliber") or "")
        self.lbl_pool.setText(f"股票池：{result.pool_text or '--'}")
        if restored and not result.frames:
            self.lbl_table_hint.setText("这是上次扫描的结果；选中标的时会重新取该股日K（行情源在线时）")

    # ---------------------------------------------------------------- 表格
    def _collect_rows(self) -> list[dict]:
        mode = self.combo.currentData()
        result = self.result
        rows: list[dict] = []
        if result is None:
            return rows
        if mode == "pool":
            for record in self.repo.limitup_pool():
                score = record.get("score") or {}
                rows.append({
                    "kind": "pool",
                    "symbol": record.get("symbol"),
                    "code": record.get("code") or "",
                    "name": record.get("name") or "",
                    "sector": record.get("sector") or "",
                    "stage": record.get("stage") or "",
                    "stage_label": STATE_LABEL.get(record.get("state"), record.get("state") or ""),
                    "score": score.get("normalized"),
                    "grade": score.get("grade"),
                    "close": record.get("last_close"),
                    "pct": None,
                    "vol": record.get("last_vol_ratio5"),
                    "turnover": record.get("last_turnover"),
                    "record": record,
                    "item": None,
                })
            rows.sort(key=lambda r: -(r.get("score") or 0))
            return rows
        if mode == "pick":
            source = list(result.picks)
        elif mode == "all":
            source = result.all_candidates
        else:
            source = list(result.groups.get(mode) or [])
        for item in source:
            snap = item["snap"]
            score = item["score"]
            rows.append({
                "kind": "candidate",
                "symbol": snap["symbol"],
                "code": snap["code"],
                "name": snap["name"],
                "sector": snap.get("sector") or "",
                "stage": item["stage"],
                "stage_label": STAGE_LABEL[item["stage"]],
                "score": score.get("normalized"),
                "grade": score.get("grade"),
                "close": snap.get("close"),
                "pct": snap.get("pct"),
                "vol": snap.get("vol_ratio5"),
                "turnover": snap.get("turnover"),
                "record": None,
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
            self.table.setItem(index, 3, _NumCell(row["stage_label"], row["stage"],
                                                 _stage_color(row["stage"]), align_right=False))
            self.table.setItem(index, 4, _NumCell(_num(row["score"], 1), row["score"] or 0,
                                                 _grade_color(row["grade"] or "")))
            self.table.setItem(index, 5, _NumCell(row["grade"] or "--",
                                                 {"S级": 5, "A级": 4, "B级": 3, "C级": 2}.get(
                                                     row["grade"] or "", 0),
                                                 _grade_color(row["grade"] or ""),
                                                 align_right=False))
            self.table.setItem(index, 6, _NumCell(_num(row["close"]), row["close"], COLORS["text"]))
            pct = row["pct"]
            self.table.setItem(index, 7, _NumCell(
                "--" if pct is None else f"{pct:+.2f}%", pct if pct is not None else 0,
                COLORS["up"] if (pct or 0) > 0 else COLORS["down"] if (pct or 0) < 0
                else COLORS["text_dim"]))
            self.table.setItem(index, 8, _NumCell(_num(row["vol"]), row["vol"] or 0,
                                                 COLORS["up"] if (row["vol"] or 0) >= 3
                                                 else COLORS["text_dim"]))
            self.table.setItem(index, 9, _NumCell(_num(row["turnover"]), row["turnover"] or 0,
                                                 COLORS["text_dim"]))
            self.table.setItem(index, 10, _NumCell(row["sector"] or "--", row["sector"] or "",
                                                  COLORS["text_dim"], align_right=False))
            self.table.item(index, 0).setToolTip(self._tooltip(row))
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.AscendingOrder)
        if self._rows:
            self.table.selectRow(0)
            self._on_selected()
        else:
            self._show_empty_detail()
        self.lbl_table_hint.setText(
            f"共 {len(self._rows)} 只 · 双击行在单股视图打开 · 阶段/评分按日线收盘口径")

    @staticmethod
    def _tooltip(row: dict) -> str:
        lines = [f"{row['name']}（{row['symbol']}）", f"阶段：{row['stage_label']}"]
        if row.get("item"):
            item = row["item"]
            lines.append(item.get("reason") or "")
            lines.append(f"支撑 {_num(item['plan'].get('support'))} · "
                         f"止损 {_num(item['plan'].get('stop'))} · "
                         f"目标 {_num(item['plan'].get('target1'))}/{_num(item['plan'].get('target2'))}")
        else:
            record = row.get("record") or {}
            lines.append(f"状态：{STATE_LABEL.get(record.get('state'), '--')}")
            lines.append(f"首板日：{record.get('first_board_date') or '--'}")
        return "\n".join([x for x in lines if x])

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
        self.detail_title.setText(f"{row['name']}  {row['symbol']}")
        self.detail_badge.setText(f"{row['stage_label']} · {_num(row['score'], 1)} 分 "
                                  f"{row['grade'] or ''}")
        self.detail_badge.setStyleSheet(f"color:{_stage_color(row['stage'])};")
        self.btn_add.setEnabled(True)
        self.btn_open.setEnabled(True)
        self.btn_tdx.set_symbol(row["symbol"])

        item = row.get("item")
        record = row.get("record")
        if item is not None:
            snap, plan, score = item["snap"], item["plan"], item["score"]
            labels = self.plan_labels
            state_text = item.get("state_label") or ""
            labels["stage"].setText(
                item["stage_label"] if state_text in ("", item["stage_label"])
                else f"{item['stage_label']}→{state_text}")
            labels["board"].setText(str(item.get("first_board_date") or "--"))
            labels["score"].setText(f"{_num(score.get('normalized'), 1)} / "
                                    f"{score.get('grade') or '--'}")
            labels["support"].setText(_num(plan.get("support")))
            labels["stop"].setText(_num(plan.get("stop")))
            labels["target"].setText(f"{_num(plan.get('target1'))} / {_num(plan.get('target2'))}")
            labels["close"].setText(_num(snap.get("close")))
            labels["vol"].setText(f"{_num(snap.get('vol_ratio5'))} 倍")
            labels["turnover"].setText(f"{_num(snap.get('turnover'))}%")
            labels["risk"].setText(str(item.get("risk") or "--"))
            labels["complete"].setText(f"{_num(score.get('completeness'), 2)} / 置信 "
                                       f"{score.get('confidence') or '--'}")
            labels["plan"].setText(plan.get("invalidation") or "--")
            labels["plan"].setToolTip(plan.get("invalidation") or "")
            labels["trigger"].setText(item.get("next_trigger") or "--")

            for key, mark in self.check_labels.items():
                ok = bool(item.get("checks", {}).get(key))
                mark.setText("✓" if ok else "✗")
                mark.setStyleSheet(f"color:{COLORS['down'] if ok else COLORS['up']};")
            carry = item.get("carry") or {}
            launch = item.get("launch") or {}
            extra = []
            if carry.get("carry_type"):
                extra.append(f"承接 {carry['carry_type']}")
            if launch:
                done = sum(1 for v in (launch.get("checks") or {}).values() if v)
                total = len(launch.get("checks") or {})
                extra.append(f"启动 {'确认' if launch.get('confirmed') else '待确认'}"
                             f"（{done}/{total}）")
            self.lbl_extra.setText("承接 / 启动：" + (" · ".join(extra) if extra else "未发生"))
            self.lbl_extra.setToolTip(
                (carry.get("detail") or "") + ("\n" + (launch.get("detail") or "")
                                               if launch else ""))

            detail = score.get("detail") or {}
            for key, value in self.score_labels.items():
                points = (detail.get(key) or {}).get("points", 0)
                value.setText(f"{_num(points, 1)}/{WEIGHTS[key]:.0f}")
            self.score_total.setText(f"{_num(score.get('normalized'), 1)}")
            self.score_total.setStyleSheet(f"color:{_grade_color(score.get('grade') or '')};")
            verified = [DIM_LABEL.get(k, k) for k in score.get("verified_dims") or []]
            unverified = [DIM_LABEL.get(k, k) for k in score.get("unverified") or []]
            self.lbl_gate.setText(
                f"门控：已验证 {len(verified)}/{len(DIM_LABEL)} 维 · 未验证 {len(unverified)} 项")
            self.lbl_gate.setToolTip(
                "已验证：" + ("、".join(verified) or "无") + chr(10)
                + "未验证：" + ("、".join(unverified) or "无") + chr(10)
                + f"原始分 {score.get('raw')} / 已验证满分 {score.get('max_raw')}")
            self._draw_chart(row["symbol"], plan, snap)
        else:
            record = record or {}
            score = record.get("score") or {}
            plan = record.get("plan") or {}
            labels = self.plan_labels
            labels["stage"].setText(STATE_LABEL.get(record.get("state"), "--"))
            labels["board"].setText(str(record.get("first_board_date") or "--"))
            labels["score"].setText(f"{_num(score.get('normalized'), 1)} / "
                                    f"{score.get('grade') or '--'}")
            labels["support"].setText(_num(plan.get("support")))
            labels["stop"].setText(_num(plan.get("stop")))
            labels["target"].setText(f"{_num(plan.get('target1'))} / {_num(plan.get('target2'))}")
            labels["close"].setText(_num(record.get("last_close")))
            labels["vol"].setText(f"{_num(record.get('last_vol_ratio5'))} 倍")
            labels["turnover"].setText(f"{_num(record.get('last_turnover'))}%")
            labels["risk"].setText(str(record.get("risk_level") or "--"))
            labels["complete"].setText(_num(score.get("completeness"), 2))
            labels["plan"].setText(plan.get("invalidation") or "--")
            labels["trigger"].setText("股票池历史记录（非当日扫描）")
            for mark in self.check_labels.values():
                mark.setText("—")
                mark.setStyleSheet(f"color:{COLORS['text_muted']};")
            self.lbl_extra.setText("承接 / 启动：股票池历史记录（非当日扫描）")
            self.lbl_extra.setToolTip("")
            for key in self.score_labels:
                points = ((score.get("detail") or {}).get(key) or {}).get("points")
                self.score_labels[key].setText(
                    f"{_num(points, 1) if points is not None else '--'}/{WEIGHTS[key]:.0f}")
            self.score_total.setText(_num(score.get("normalized"), 1))
            self.lbl_gate.setText("门控：来自首板日评分快照")
            self._draw_chart(row["symbol"], plan, None)

    def _draw_chart(self, symbol: str, plan: dict | None, snap: dict | None) -> None:
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
                bar_time=datetime.strptime(str(item["date"]), "%Y%m%d"),
                open=float(item["open"]), high=float(item["high"]), low=float(item["low"]),
                close=float(item["close"]), volume=int(item["volume"]),
                amount=float(item["amount"]), data_level="C", source="tdx:lday",
                quality_flag="exchange")
            for item in frame
        ]
        self.set_chart_bars(symbol, bars)

    def set_chart_bars(self, symbol: str, bars: list) -> None:
        """主窗口取到日K后回填。只对应当前选中标的。"""
        if self._current is None or self._current["symbol"] != symbol or not bars:
            return
        self.chart.set_bars(bars, "日K", "C", "exchange")
        item = self._current.get("item")
        plan = item["plan"] if item else (self._current.get("record") or {}).get("plan") or {}
        levels = []
        if plan.get("support"):
            levels.append({"price": plan["support"], "label": "支撑", "color": COLORS["info"]})
        if plan.get("stop"):
            levels.append({"price": plan["stop"], "label": "止损", "color": COLORS["down"]})
        if plan.get("target1"):
            levels.append({"price": plan["target1"], "label": "1.5R", "color": COLORS["up"]})
        if plan.get("target2"):
            levels.append({"price": plan["target2"], "label": "2.5R", "color": COLORS["up"]})
        self.chart.set_price_levels(levels)

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
        for key, value in self.score_labels.items():
            value.setText(f"--/{WEIGHTS[key]:.0f}")
        self.score_total.setText("--")
        self.lbl_gate.setText("门控：--")
        self.lbl_extra.setText("承接 / 启动：--")
        self.chart.set_price_levels([])
        self.chart.set_bars([], "日K", "C")
        self.chart.empty_reason = "没有标的：先扫描，或把阶段切回「全部候选」"
        self.chart.update()

    # ---------------------------------------------------------------- 落盘/导出
    def _persist(self) -> None:
        if self.result is None:
            return
        try:
            self.repo.set_setting(SETTING_KEY,
                                  json.dumps(self.result.to_payload(), ensure_ascii=False))
        except Exception as exc:            # noqa: BLE001 - 落盘失败不能影响使用
            print(f"[limitup] 保存扫描结果失败: {exc}")

    def _restore(self) -> None:
        raw = self.repo.get_setting(SETTING_KEY, "") or ""
        if not raw:
            return
        try:
            result = ScanResult.from_payload(json.loads(raw))
        except (ValueError, TypeError) as exc:
            print(f"[limitup] 上次扫描结果无法解析，忽略: {exc}")
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
        path = outdir / f"limitup_{stamp}.html"
        try:
            path.write_text(render_report(self.result, self.config), encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.message.emit(f"涨停四阶段报告已写入 {path}")
        QMessageBox.information(self, "导出完成", f"已生成单文件报告：\n{path}")
