"""主窗口：装配数据源、轮询、分组、图表、预警，并负责状态落盘与恢复。"""
from __future__ import annotations

import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QSplitter, QStackedWidget, QVBoxLayout, QWidget,
)

from app.adapters import build_adapter, build_intraday_adapter, build_lhb_adapter
from app.adapters.base import Capabilities, DataSourceError
from app.adapters.money_flow import MoneyFlowAdapter
from app.adapters.eastmoney_money_flow import EastmoneyMoneyFlowAdapter
from app.config import load_config, sqlite_path_from_url
from app.db.models import Quote
from app.db.repo import Repo
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.alert_service import AlertService, format_alert
from app.services.intraday import (
    EM_FLAG, MINUTE_FLAGS, IntradaySampler, MinuteFeed, is_minute_flag,
    minute_source_meta, points_from_sampled_bars,
)
from app.services.intraday_poller import IntradayPoller
from app.services.large_order import LargeOrder, LargeOrderDetector
from app.services.market_clock import MarketClock
from app.services.quote_poller import QuotePoller
from app.services.task_runner import BackgroundJobs
from app.services.universe import UniverseService
from app.ui.dialogs import AMOUNT_RULES, INTRADAY_RULES_UI, AlertRuleDialog, ImportSymbolsDialog
from app.ui.charts import CANDLE_SCHEMES, TOTAL_SLOTS
from app.ui.panels import (
    AlertRulePanel, GroupPanel, LogPanel, OrderBookPanel, TradePanel, _label,
)
from app.ui.limitup_view import LimitupView
from app.ui.pullback_view import PullbackView
from app.ui.theme import COLORS, load_qss
from app.ui.views import GRID_SIZES, MultiGridView, SingleStockView

SETTING_KEYS = {
    "group": "ui.current_group",
    "symbol": "ui.current_symbol",
    "mode": "ui.mode",
    "grid_size": "ui.grid_size",
    "period": "ui.period",
    "candle_scheme": "ui.candle_scheme",
}

PERIOD_LABELS = {"1d": "日K", "1w": "周K", "1M": "月K", "intraday": "采样分时"}


class MainWindow(QMainWindow):
    def __init__(self, config_path: str = "config.yaml", adapter=None) -> None:
        super().__init__()
        self.config = load_config(config_path)
        db_url = (self.config.get("database") or {}).get("url", "sqlite:///./data/dragon_hunter.db")
        self.db = Database(sqlite_path_from_url(db_url))
        self.db.init_schema()
        self.db.seed_default_groups()
        self.repo = Repo(self.db)

        self.adapter = adapter or build_adapter(self.config)
        # 龙虎榜是独立数据源（交易所披露走东方财富），与本地行情源解耦；未启用时返回 None
        try:
            self.lhb_adapter = build_lhb_adapter(self.config)
        except DataSourceError as exc:
            self.lhb_adapter = None
            self._lhb_error = exc.message
        else:
            self._lhb_error = ""
        self._lhb_dialog = None
        # 分时改走东财分钟线；装配失败（未启用/未知 provider）时退回快照采样
        try:
            self.intraday_adapter = build_intraday_adapter(self.config)
        except DataSourceError as exc:
            self.intraday_adapter = None
            self._intraday_error = exc.message
        else:
            self._intraday_error = ""
        # 分时来源：东财或 free-stockdb。缺省标记取适配器声明的那个，
        # 实际每个点的标记以 Trends.minute_flag 为准（回落时可能换源）。
        self.minute_flag = getattr(self.intraday_adapter, "minute_flag", "") or EM_FLAG
        self.minute_feed = MinuteFeed(
            flag=self.minute_flag,
            source_label=getattr(self.intraday_adapter, "source_label", None),
        )
        # 资金流（日K 下方的主力净额副图）：只有 stockdb 一家能给，没有回落源。
        # 构造不取数，取不到就在副图里如实显示原因，不影响 K 线渲染。
        self.money_flow_adapter = MoneyFlowAdapter(self.config)
        self._money_flow_symbol = ""     # 已填充过副图的股票，避免重复提交任务
        # 分时资金流（分时页下方的五线副图）：东财 fflow/kline，与分时图同一只票同一天。
        # 两条资金流通道各管一摊：日级按交易日（stockdb），分钟级按分钟（东财），
        # 口径、单位、失败原因都不同，不能互相顶替。
        self.intraday_flow_adapter = EastmoneyMoneyFlowAdapter(self.config)
        iflow_cfg = self.config.get("money_flow_intraday") or {}
        self._iflow_interval = max(5.0, float(iflow_cfg.get("refresh_sec", 15) or 15))
        self._iflow_symbol = ""          # 当前分时副图对应的股票
        self._iflow_at: dict[str, float] = {}   # 代码 -> 上次真正发请求的 monotonic
        self._iflow_error = ""           # 上次失败原因，避免同一个错误每 15 秒刷一次状态栏
        self.caps: Capabilities = self.adapter.capabilities
        ds = self.config.get("datasource") or {}
        alerts_cfg = self.config.get("alerts") or {}
        lo_cfg = self.config.get("large_order") or {}
        self.latency_warn_ms = int(ds.get("latency_warn_ms", 5000))
        self.latency_abort_ms = int(ds.get("latency_abort_ms", 30000))
        self.clock = MarketClock((self.config.get("market") or {}).get("calendar_source", "approximate"))
        self.sampler = IntradaySampler()
        self.detector = LargeOrderDetector(
            tiers=lo_cfg.get("tiers"),
            streak_count=int(lo_cfg.get("streak_count", 3)),
            streak_multiplier=float(lo_cfg.get("streak_amount_multiplier", 2)),
        )
        self.universe = UniverseService(self.repo, self.adapter, self.config)
        self.jobs = BackgroundJobs()
        self.alert_service = AlertService(
            self.repo,
            AlertEngine(
                cooldown_sec=int(alerts_cfg.get("default_cooldown_sec", 300)),
                per_symbol_per_minute=int(alerts_cfg.get("per_symbol_per_minute_limit", 5)),
            ),
            require_trading=bool(alerts_cfg.get("require_trading", True)),
        )
        self.poller = QuotePoller(
            self.adapter, self.sampler, self.repo,
            interval_sec=float(ds.get("poll_interval_sec", 3)),
            persist=True, detector=self.detector,
        )
        # 有东财分钟线时，分时与区间大单以分钟线为准；采样只做内存兜底
        self.poller.sampler_owns = lambda symbol: not self.minute_feed.has_data(symbol)

        intraday_cfg = self.config.get("intraday") or {}
        self.intraday_poller = None
        if self.intraday_adapter is not None:
            self.intraday_poller = IntradayPoller(
                self.intraday_adapter, self.minute_feed, self.repo,
                refresh_sec=float(intraday_cfg.get("refresh_sec", 15)),
                grid_cycles=int(intraday_cfg.get("grid_refresh_cycles", 4)),
                detector=self.detector, persist=True,
                is_trading=lambda: self.clock.status().is_trading,
            )

        self.current_group_id: int | None = None
        self.current_symbol: str = ""
        # 单股精盯页当前已加载的股票，用于判断「同一只重复点击」时直接复用
        self._single_symbol: str = ""
        self.mode = "single"
        self.grid_size = 4
        self.chart_period = "1d"
        self.grid_symbols: list[str] = []
        self._poll_count = 0
        self._trade_loaded_symbol = ""
        self._trade_live_symbol = ""
        self._intraday_down = False
        self._universe_ready = False
        self._loading_groups = False
        self._last_status_label = ""

        self.setWindowTitle("寻龙盯盘 Dragon Hunter")
        self.resize(1680, 980)
        self.setStyleSheet(load_qss())
        self._build_ui()
        self._build_menu()
        self._bind_shortcuts()
        self._restore_state()
        self._refresh_groups()

        self.poller.start()
        if self.intraday_poller is not None:
            self.intraday_poller.start()
        self.universe.load_async(done=lambda summary: None)

        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._on_ui_tick)
        self._ui_timer.start(300)
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._refresh_status)
        self._clock_timer.start(1000)
        self._refresh_status()

    # ================================================================ 构建 UI
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(4)
        root.addWidget(self._build_topbar())

        self.splitter = QSplitter(Qt.Horizontal)
        self.left = GroupPanel()
        self.left.setMinimumWidth(280)
        self.left.setMaximumWidth(460)
        self.left.group_changed.connect(self._on_group_changed)
        self.left.new_group_requested.connect(self._on_new_group)
        self.left.rename_requested.connect(self._on_rename_group)
        self.left.copy_requested.connect(self._on_copy_group)
        self.left.delete_requested.connect(self._on_delete_group)
        self.left.order_changed.connect(self._on_groups_reordered)
        self.left.picked.connect(self._on_symbol_picked)
        self.left.add_requested.connect(lambda symbol: self._add_symbols([symbol]))
        self.left.remove_requested.connect(self._on_remove_symbols)

        self.single_view = SingleStockView(self.config)
        self.single_view.period_changed.connect(self._on_period_changed)
        self.single_view.scheme_changed.connect(self._on_candle_scheme_changed)
        self.single_view.time_clicked.connect(self._on_chart_time_clicked)
        self.single_view.tdx_message.connect(self._on_tdx_message)
        self.grid_view = MultiGridView()
        self.grid_view.cell_activated.connect(self._on_symbol_picked)
        # 强势回调选股：读本地日线全市场扫描，独立线程，不占用盘中轮询
        self.pullback_view = PullbackView(self.config, self.repo)
        self.pullback_view.symbol_activated.connect(self._on_pullback_symbol)
        self.pullback_view.add_requested.connect(self._on_pullback_add)
        self.pullback_view.bars_needed.connect(self._load_pullback_bars)
        self.pullback_view.message.connect(self._on_pullback_message)
        # 涨停四阶段选股：同样是本地日线 + 独立线程，带跨日股票池状态机
        self.limitup_view = LimitupView(self.config, self.repo)
        self.limitup_view.symbol_activated.connect(self._on_limitup_symbol)
        self.limitup_view.add_requested.connect(self._on_limitup_add)
        self.limitup_view.bars_needed.connect(self._load_limitup_bars)
        self.limitup_view.message.connect(self._on_limitup_message)
        self.center = QStackedWidget()
        self.center.addWidget(self.single_view)
        self.center.addWidget(self.grid_view)
        self.center.addWidget(self.pullback_view)
        self.center.addWidget(self.limitup_view)

        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.center)
        self.right_panel = self._build_right()
        self.splitter.addWidget(self.right_panel)
        self.splitter.setSizes([320, 1040, 320])
        root.addWidget(self.splitter, 5)

        self.logs = LogPanel()
        self.logs.note_saved.connect(self._on_note_saved)
        self.logs.setMaximumHeight(170)
        root.addWidget(self.logs, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(12)

        self.lbl_market = QLabel("—")
        self.lbl_market.setProperty("role", "metric")
        self.lbl_source = _label("数据源：—", "warn")
        self.lbl_clock = QLabel("--:--:--")
        self.lbl_clock.setProperty("role", "metric")
        self.lbl_pool = _label("股票池 --", "muted")

        layout.addWidget(self.lbl_market)
        layout.addWidget(self.lbl_source)
        layout.addWidget(self.lbl_clock)
        layout.addWidget(self.lbl_pool)
        layout.addStretch(1)

        self.btn_single = QPushButton("单股精盯")
        self.btn_single.setCheckable(True)
        self.btn_single.setChecked(True)
        self.btn_single.clicked.connect(lambda: self._set_mode("single"))
        self.btn_grid = QPushButton("多股对比")
        self.btn_grid.setCheckable(True)
        self.btn_grid.clicked.connect(lambda: self._set_mode("grid"))
        self.btn_pullback = QPushButton("强势回调")
        self.btn_pullback.setCheckable(True)
        self.btn_pullback.setToolTip(
            "全市场日线扫描：近10日涨停 + 创阶段新高 + 回调到 MA10/颈线 + 缩量止跌\n"
            "收盘后口径，不是盘中实时信号（数据来自本地通达信日线）")
        self.btn_pullback.clicked.connect(lambda: self._set_mode("pullback"))
        self.btn_limitup = QPushButton("涨停四阶")
        self.btn_limitup.setCheckable(True)
        self.btn_limitup.setToolTip(
            "四阶段选股：首板发现 → 三倍量确认 → 次日承接 → 二次启动\n"
            "收盘后日线口径（本地通达信 vipdoc），带跨日股票池状态机")
        self.btn_limitup.clicked.connect(lambda: self._set_mode("limitup"))
        self.grid_combo = QComboBox()
        for size in GRID_SIZES:
            self.grid_combo.addItem(f"{size} 格", size)
        self.grid_combo.setCurrentIndex(GRID_SIZES.index(4))
        self.grid_combo.currentIndexChanged.connect(self._on_grid_size_changed)
        self.grid_combo.setVisible(False)

        self.btn_lhb = QPushButton("龙虎榜")
        self.btn_lhb.setToolTip("查看东方财富公开披露的龙虎榜（C 级 · T 日盘后）")
        self.btn_lhb.clicked.connect(self._open_lhb)
        self.btn_lhb.setEnabled(self.lhb_adapter is not None)
        if self.lhb_adapter is None:
            self.btn_lhb.setToolTip(self._lhb_error or "未启用龙虎榜数据源（配置 lhb.enabled）")

        layout.addWidget(self.btn_single)
        layout.addWidget(self.btn_grid)
        layout.addWidget(self.btn_pullback)
        layout.addWidget(self.btn_limitup)
        layout.addWidget(self.grid_combo)
        layout.addWidget(self.btn_lhb)
        return bar

    def _build_right(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(292)
        panel.setMaximumWidth(440)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 2)
        layout.setSpacing(5)

        self.order_book = OrderBookPanel()
        if self.caps.order_book_levels:
            self.order_book.badge.setText(f"{self.caps.order_book_levels} 档")
        else:
            self.order_book.set_unsupported("该数据源不提供盘口数据。")
        self.order_book.setFixedHeight(280)      # 10 档行高固定，避免被挤压后只显示半截

        self.trade_panel = TradePanel()
        self.trade_panel.set_interval(self.poller.interval)
        self.trade_panel.set_tick_supported(
            bool(self.caps.tick),
            "tqcenter 的 period='tick' 返回 error=-5，本机无逐笔接口。\n\n"
            "替代口径：\n"
            "· 采样成交页＝相邻快照差分，列名标「区间」；\n"
            "· 区间大单页＝区间累计成交额分档，不写「单笔」；\n"
            "· 点击任一行会联动分时图与日K定位。")
        self.trade_panel.time_focused.connect(self._focus_time)
        self.trade_panel.setMinimumHeight(176)

        self.alert_panel = AlertRulePanel()
        self.alert_panel.add_requested.connect(lambda _: self._on_add_alert())
        self.alert_panel.delete_requested.connect(self._on_delete_alert)
        self.alert_panel.toggle_requested.connect(self._on_toggle_alert)
        self.alert_panel.refresh_requested.connect(self._refresh_alert_rules)
        self.alert_panel.setMinimumHeight(132)

        # 三块用竖向分割器装：高度不够时只会挤压（各自出滚动条），不会像 QVBoxLayout 那样
        # 因为固定高度被压缩而互相重叠。分割条也可手动拖，交易员能按自己习惯分配空间。
        self.right_split = QSplitter(Qt.Vertical)
        self.right_split.setChildrenCollapsible(False)
        self.right_split.setHandleWidth(5)
        self.right_split.addWidget(self.order_book)
        self.right_split.addWidget(self.trade_panel)
        self.right_split.addWidget(self.alert_panel)
        self.right_split.setStretchFactor(0, 0)
        self.right_split.setStretchFactor(1, 3)
        self.right_split.setStretchFactor(2, 2)
        self.right_split.setSizes([280, 250, 214])
        layout.addWidget(self.right_split, 1)
        return panel

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件")
        backup = QAction("备份数据库", self)
        backup.triggered.connect(self._on_backup)
        file_menu.addAction(backup)
        import_action = QAction("批量导入股票…", self)
        import_action.triggered.connect(self._on_import)
        file_menu.addAction(import_action)
        file_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("视图")
        reset = QAction("恢复默认布局", self)
        reset.triggered.connect(self._reset_layout)
        view_menu.addAction(reset)

        help_menu = self.menuBar().addMenu("帮助")
        caps = QAction("数据源能力", self)
        caps.triggered.connect(self._show_caps)
        help_menu.addAction(caps)

    def _bind_shortcuts(self) -> None:
        for key, period in (("1", "intraday"), ("2", "1d"), ("3", "1w"), ("4", "1M")):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(lambda p=period: self._on_period_changed(p))
        refresh = QShortcut(QKeySequence("F5"), self)
        refresh.activated.connect(self._on_manual_refresh)

    # ================================================================ 状态恢复/保存
    def _restore_state(self) -> None:
        self.mode = self.repo.get_setting(SETTING_KEYS["mode"], "single") or "single"
        self.grid_size = int(self.repo.get_setting(SETTING_KEYS["grid_size"], "4") or 4)
        self.chart_period = self.repo.get_setting(SETTING_KEYS["period"], "1d") or "1d"
        self.candle_scheme = self.repo.get_setting(
            SETTING_KEYS["candle_scheme"], "change") or "change"
        # 三个视图的 K 线共用一种配色，免得同一屏里两套颜色口径
        self._apply_candle_scheme(self.candle_scheme)
        saved_symbol = self.repo.get_setting(SETTING_KEYS["symbol"], "") or ""
        saved_group = self.repo.get_setting(SETTING_KEYS["group"], "") or ""

        layout = self.repo.get_layout("global", "main")
        geometry = layout.get("window_geometry")
        if geometry:
            try:
                self.restoreGeometry(bytes.fromhex(geometry))
            except ValueError:
                pass
        sizes = layout.get("splitter")
        if isinstance(sizes, list) and len(sizes) == 3:
            self.splitter.setSizes([int(v) for v in sizes])
        bottom = layout.get("bottom_height")
        if bottom:
            self.logs.setMaximumHeight(max(120, int(bottom)))

        grid_layout = self.repo.get_layout("global", "grid")
        self.grid_symbols = list(grid_layout.get("symbols") or [])

        self._pending_group = int(saved_group) if saved_group.isdigit() else None
        self._pending_symbol = saved_symbol
        if self.grid_size not in GRID_SIZES:
            self.grid_size = 4
        self.grid_combo.setCurrentIndex(GRID_SIZES.index(self.grid_size))
        self.grid_view.set_size(self.grid_size)
        self._apply_mode()
        self.single_view.set_period(self.chart_period)

    def _save_state(self) -> None:
        try:
            self.repo.set_setting(SETTING_KEYS["mode"], self.mode)
            self.repo.set_setting(SETTING_KEYS["grid_size"], self.grid_size)
            self.repo.set_setting(SETTING_KEYS["period"], self.chart_period)
            if self.current_group_id:
                self.repo.set_setting(SETTING_KEYS["group"], self.current_group_id)
            if self.current_symbol:
                self.repo.set_setting(SETTING_KEYS["symbol"], self.current_symbol)
            self.repo.save_layout("global", "main", {
                "window_geometry": bytes(self.saveGeometry()).hex(),
                "splitter": self.splitter.sizes(),
                "bottom_height": self.logs.maximumHeight(),
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            })
            self.repo.save_layout("global", "grid", {
                "symbols": self.grid_view.symbols(),
                "size": self.grid_size,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
            })
        except Exception as exc:  # 保存失败不能阻断退出
            print(f"[ui] 保存状态失败: {exc}")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._save_state()
        self.poller.stop()
        if self.intraday_poller is not None:
            self.intraday_poller.stop()
        self.pullback_view.stop()
        self.limitup_view.stop()
        self.jobs.stop()
        try:
            self.adapter.disconnect()
        except Exception:
            pass
        super().closeEvent(event)

    def show_demo_banner(self, text: str) -> None:
        """截图/联调时在顶栏挂一条显眼的演示提示；正常运行时不会调用。"""
        bar = self.lbl_pool.parentWidget()
        if bar is None or bar.layout() is None:
            return
        if not hasattr(self, "_demo_label"):
            self._demo_label = _label("", "warn")
            bar.layout().insertWidget(0, self._demo_label)
        self._demo_label.setText(text)

    def _reset_layout(self) -> None:
        self.splitter.setSizes([320, 1040, 320])
        self.logs.setMaximumHeight(170)
        self.repo.save_layout("global", "main", {})
        self.resize(1680, 980)
        self.statusBar().showMessage("已恢复默认布局", 3000)

    # ================================================================ 分组
    def _refresh_groups(self, select_group: int | None = None, select_symbol: str | None = None) -> None:
        groups = self.repo.groups()
        target = select_group if select_group is not None else (
            self.current_group_id or getattr(self, "_pending_group", None))
        self._loading_groups = True
        self.left.load_groups(groups, target)
        self._loading_groups = False
        self.current_group_id = self.left.current_group_id()
        if self.current_group_id is None and groups:
            self.current_group_id = groups[0]["id"]
        self._load_group(select_symbol or getattr(self, "_pending_symbol", "") or None)

    def _on_group_changed(self, group_id: int) -> None:
        if self._loading_groups:
            self.current_group_id = group_id
            return
        if group_id == self.current_group_id:
            return
        self.current_group_id = group_id
        self._pending_symbol = ""
        self._load_group()
        self.repo.set_setting(SETTING_KEYS["group"], group_id)

    def _load_group(self, select_symbol: str | None = None) -> None:
        if self.current_group_id is None:
            self.left.table.set_symbols([])
            self.left.set_stock_hint("还没有分组。点左上角「＋」新建，再用搜索框加入股票。")
            return
        symbols = self.repo.group_symbols(self.current_group_id)
        names = self.repo.stock_names(symbols)
        self.left.table.set_symbols(symbols, names)

        cached = self.repo.cached_quotes(symbols)
        for symbol, row in cached.items():
            self.left.table.update_quote(_quote_from_row(row, names.get(symbol, "")))
        self.single_view.info.clear()

        if not symbols:
            self.left.set_stock_hint(
                f"「{self.left.current_group_name()}」为空。搜索框选中后 Ctrl+Enter 加入，或菜单「文件 → 批量导入股票」。")
            self.current_symbol = ""
            # 分组空了，单股页已无有效标的：清掉复用标记，下次点击必须重新加载
            self._single_symbol = ""
            self.single_view.clear_symbol()
            self.poller.update([], None)
            if self.intraday_poller is not None:
                self.intraday_poller.update([], None)
            self.single_view.show_error("该分组暂无股票")
            return

        self.left.set_stock_hint(f"共 {len(symbols)} 只 · 轮询间隔 {self.poller.interval:.0f}s")
        target = select_symbol if select_symbol in symbols else (
            self.current_symbol if self.current_symbol in symbols else symbols[0])
        self.poller.update(symbols, target)
        if self.intraday_poller is not None:
            self.intraday_poller.update(symbols, target)
        self._select_symbol(target)

    def _on_groups_reordered(self, ordered_ids: list[int]) -> None:
        self.repo.reorder_groups(ordered_ids)
        self._refresh_groups(self.current_group_id)

    def _on_new_group(self, name: str) -> None:
        try:
            group_id = self.repo.create_group(name)
        except Exception as exc:
            QMessageBox.warning(self, "新建分组失败", str(exc))
            return
        self._refresh_groups(group_id)

    def _on_rename_group(self, group_id: int, name: str) -> None:
        try:
            self.repo.rename_group(group_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "重命名失败", str(exc))
            return
        self._refresh_groups(group_id)

    def _on_copy_group(self, group_id: int, name: str) -> None:
        try:
            new_id = self.repo.copy_group(group_id, name)
        except Exception as exc:
            QMessageBox.warning(self, "复制失败", str(exc))
            return
        self._refresh_groups(new_id)

    def _on_delete_group(self, group_id: int) -> None:
        self.repo.delete_group(group_id)
        self.current_group_id = None
        self._pending_group = None
        self._pending_symbol = ""
        self._refresh_groups()

    # ================================================================ 单股精盯
    def _on_symbol_picked(self, symbol: str) -> None:
        """列表单击/双击、搜索框、多股网格的选股入口，统一走单股精盯。"""
        self.open_symbol(symbol)

    def open_symbol(self, symbol: str, source: str = "") -> None:
        """打开单股精盯页并加载该股。

        跳转方式：不弹窗，直接把中间区域切到单股精盯页（QStackedWidget），
        顶部「单股精盯」按钮同步点亮，右栏盘口/成交/预警随页面恢复显示。
        参数：symbol 为内部代码（如 600519.SH）；不在当前分组时先静默加入。
        复用：若该股已是单股页当前标的，只把页面切回来，不重建图表、不重复请求。
        """
        if not symbol:
            return
        symbols = self.left.table.symbols()
        if symbol not in symbols and self.current_group_id is not None:
            self._add_symbols([symbol], quiet=True)
            symbols = self.left.table.symbols()
        if symbol in symbols:
            self.left.table.select_symbol(symbol)

        name = self.repo.stock_names([symbol]).get(symbol, "") or symbol
        reused = bool(self._single_symbol) and symbol == self._single_symbol
        switched = self.mode != "single"
        self._set_mode("single")

        if reused:
            # 已打开同一只：把焦点还给单股页即可，K 线缩放/标记/分时都不重置
            self.single_view.setFocus(Qt.OtherFocusReason)
            tip = f"「{name}」已在单股精盯"
            if switched:
                tip += "，已切回"
            self.statusBar().showMessage(tip, 2500)
            return

        self._select_symbol(symbol)
        prefix = f"{source} → " if source else ""
        self.statusBar().showMessage(f"{prefix}单股精盯：{name} {symbol}", 3000)

    def _select_symbol(self, symbol: str) -> None:
        if not symbol:
            return
        self.current_symbol = symbol
        self._single_symbol = symbol
        self.repo.set_setting(SETTING_KEYS["symbol"], symbol)
        self.poller.update(self.left.table.symbols() or [symbol], symbol)
        if self.intraday_poller is not None:
            self.intraday_poller.update(self.left.table.symbols() or [symbol], symbol)
            self.intraday_poller.request_refresh()      # 切股立刻拉一次，别让用户干等一个刷新周期
        self.single_view.set_symbol(symbol)

        cached = self.repo.cached_quotes([symbol]).get(symbol)
        if cached:
            name = self.repo.stock_names([symbol]).get(symbol, "")
            self.single_view.set_quote(_quote_from_row(cached, name))
            self.single_view.info.set_quote(_quote_from_row(cached, name))
        else:
            name = self.repo.stock_names([symbol]).get(symbol, "")
            self.single_view.info.name.setText(name or symbol)
            self.single_view.info.code.setText(symbol)

        self._load_chart(symbol)
        self._load_order_book(symbol)
        self._load_trade_context(symbol)
        self._load_lhb_marks(symbol)
        self._refresh_alert_rules()

    # ---------------------------------------------------------- 成交/大单上下文
    def _intraday_series(self, symbol: str) -> list:
        """成交表/网格用的区间序列：东财分钟线优先，没有则用快照采样。"""
        return self.minute_feed.series(symbol) or self.sampler.series(symbol)

    def _restore_intraday_points(self, symbol: str, em_only: bool = False) -> tuple[list, str]:
        """内存没有分时数据时，从库里还原「最近一个有数据的交易日」。

        不能按 `date.today()` 过滤：周末/节假日打开软件时今天根本没有分钟线，
        照今天滤会得到空图，而实际上最近交易日的数据是完整的。

        `em_only=True` 是分时图的口径：只认真分钟线的行（东财或 free-stockdb）。快照采样同样往
        1m 表写点，而且经常是「今天只有开盘那 1~2 个点」，按最后一天取会把分时图
        退化成 1 个点（界面显示「数据不足」），所以必须先剔掉采样行再找最近交易日。
        """
        bars = self.repo.load_bars(symbol, "1m", 2000,
                                   quality_contains=MINUTE_FLAGS if em_only else None)
        if not bars:
            return [], ("em" if em_only else "sampled")
        last_day = bars[-1].bar_time.date()
        bars = [b for b in bars if b.bar_time.date() == last_day]
        if em_only:
            return points_from_sampled_bars(bars), "em"
        em = [b for b in bars if is_minute_flag(b.quality_flag or "")]
        use = em or bars
        return points_from_sampled_bars(use), ("em" if em else "sampled")

    def _load_trade_context(self, symbol: str) -> None:
        """切股时重建右栏：东财分钟线 > 内存采样点 > 库内最近交易日。"""
        points = self._intraday_series(symbol)
        source = "em" if self.minute_feed.has_data(symbol) else "sampled"
        if not points:
            points, source = self._restore_intraday_points(symbol)
        self._trade_loaded_symbol = symbol
        self._trade_live_symbol = symbol if self.minute_feed.has_data(symbol) else ""
        self.trade_panel.set_points(points)
        self.trade_panel.set_source_mode(source)

        live = self.detector.orders(symbol, 200)
        if live:
            orders = live
        else:
            rows = self.repo.recent_large_orders(symbol, 200)
            orders = [_order_from_row(row) for row in reversed(rows)]
        self.trade_panel.set_orders(orders)

        # 标记日跟着序列走：周末打开软件时序列是最近交易日的，用「今天」会一个标记都不显示
        chart_day = points[-1].time.date() if points else datetime.now().date()
        self.single_view.set_intraday_markers(
            [o for o in orders if o.time.date() == chart_day])
        self.single_view.set_event_marks(self.repo.large_orders_by_date(symbol, 120))
        self.single_view.set_focus_date(None)

        # 用已落库的日K补近 20 日区间，避免大单位置一直是「位置未知」
        bars = self.repo.load_bars(symbol, "1d", 400)
        if bars:
            self.detector.set_daily_range_from_bars(symbol, bars, self.detector.lookback_days)

    def _focus_time(self, moment) -> None:
        """点成交行 / 点大单行 / 点分时图，统一在这里定位。"""
        if moment is None:
            return
        self.single_view.set_intraday_focus(moment)
        self.single_view.set_focus_date(moment.date())
        self.trade_panel.select_time(moment)

    def _on_chart_time_clicked(self, moment) -> None:
        self.trade_panel.tabs.setCurrentIndex(0)
        self.trade_panel.select_time(moment)
        self.single_view.set_focus_date(moment.date())

    def _on_tdx_message(self, text: str) -> None:
        """单股页「通达信联动」的结果：只上状态栏 + 日志，失败不弹窗（与选股页一致）。"""
        if not text:
            return
        self.statusBar().showMessage(text, 8000)
        self.logs.append_event(text)

    def _load_chart(self, symbol: str, force: bool = False) -> None:
        if self.chart_period == "intraday":
            self._refresh_intraday(symbol)
            # 分时副图与主图同时起步：主图先画出来（库内已有分钟线时立即可见），
            # 资金流再异步补，谁慢谁的格子里写「加载中」，不互相等
            self._load_intraday_money_flow(symbol, force=force)
            return
        self.single_view.set_symbol(symbol)
        self.jobs.submit(
            "kline", self.adapter.get_kline, symbol, self.chart_period,
            None, None, 400, meta={"symbol": symbol, "period": self.chart_period},
        )
        # 资金流只有日K用得上：周K/月K的净额聚合口径没定义，不画也不取数。
        # 这里不清空已有序列——切到周K再切回日K时，副图能立刻恢复而不是闪一下空白。
        if self.chart_period == "1d":
            self._load_money_flow(symbol)

    def _load_money_flow(self, symbol: str) -> None:
        """拉主力净额。与 K 线同一个后台线程串行执行，不额外开请求通道。

        同一只票已经填过就不重复提交（在日K上反复点同一只股票时，
        资金流不会变，重复请求只会白等一轮队列）。
        """
        if not self.money_flow_adapter.enabled:
            self.single_view.clear_money_flow("资金流已在配置中关闭")
            return
        if symbol == self._money_flow_symbol:
            # 已经取过这只票：优先从内存缓存恢复，避免「切走再切回」时先清空再等一轮
            rows = self.money_flow_adapter.cached(symbol)
            if rows:
                self.single_view.set_money_flow(
                    rows, "stockdb 资金流", self.money_flow_adapter.data_level)
            return
        self._money_flow_symbol = symbol
        self.single_view.clear_money_flow("资金流加载中…")
        self.jobs.submit("moneyflow", self._fetch_money_flow, symbol,
                         meta={"symbol": symbol})

    def _fetch_money_flow(self, symbol: str) -> tuple[list, str]:
        """资金流取数：失败返回 (空, 原因)，不让它冒成一次 job 失败。

        副图只是日K的补充信息，资金流挂了不应该影响主图渲染；
        但原因必须原样交给副图，否则用户只看到一块空白，不知道该去启动引擎
        还是该去同步数据。
        """
        try:
            return self.money_flow_adapter.series(symbol), ""
        except DataSourceError as exc:
            return [], exc.message

    # ---------------------------------------------------------- 分时资金流
    def _load_intraday_money_flow(self, symbol: str, force: bool = False) -> None:
        """拉分时页下方的分钟资金流（东财 fflow）。

        节流分两种：
        - **换股必拉**：用户点了另一只票，就值得为它发一次请求，哪怕上一次才刚拉过；
        - **同一只票按 `money_flow_intraday.refresh_sec` 节流**：盘中累计净额在变，
          但 15 秒一次已经足够，跟着 3 秒行情轮询会把这个公开接口打到限流
          （WzSLinker 那套 500ms 轮询就是这么被封的）。
        """
        if not symbol:
            return
        if not self.intraday_flow_adapter.enabled:
            self.single_view.clear_intraday_money_flow(
                "资金流已关闭（config.yaml → money_flow_intraday.enabled: false）")
            return

        switched = symbol != self._iflow_symbol
        if switched:
            self._iflow_symbol = symbol
            cached = self.intraday_flow_adapter.cached(symbol)
            if cached and cached.points:
                # 切回来先画内存里那一版，别让格子空一轮再闪出来
                self.single_view.set_intraday_money_flow(
                    cached.points, "东方财富分时资金流", cached.data_level)
            else:
                self.single_view.clear_intraday_money_flow("资金流加载中…")

        now = time.monotonic()
        if not switched and not force and now - self._iflow_at.get(symbol, 0.0) < self._iflow_interval:
            return
        self._iflow_at[symbol] = now
        self.jobs.submit("iflow", self._fetch_intraday_money_flow, symbol,
                         meta={"symbol": symbol})

    def _fetch_intraday_money_flow(self, symbol: str):
        """后台取数：成功回序列，失败回 (None, 原因)，不抛成 job 失败。"""
        try:
            return self.intraday_flow_adapter.series(symbol), ""
        except DataSourceError as exc:
            return None, exc.message

    def _refresh_intraday(self, symbol: str) -> None:
        """分时图：只认真分钟线（内存 feed → 库内最近一个有分钟线的交易日）。

        数据源由配置决定（free-stockdb 本地库 / 东方财富），这里不写死来源，
        标题与等级都按实际给数的那个源显示，避免「数据是 A 家、图例写 B 家」。

        不再回退快照采样：采样是 3 秒区间的另一套口径（而且刚开盘只有零星几个点），
        混进分时图会把「采样点稀疏」显示成「分时没数据」，图例也说不清来源。
        """
        cached = self.repo.cached_quotes([symbol]).get(symbol)
        quote = _quote_from_row(cached) if cached else None
        meta = self.minute_feed.meta(symbol)
        points = list(self.minute_feed.series(symbol))
        if not points:
            points, _ = self._restore_intraday_points(symbol, em_only=True)

        # 实际来源以 meta 里的标记为准：主源失败回落备源时，这里必须跟着变
        flag = meta.get("minute_flag") or self.minute_flag
        _source, level, label = minute_source_meta(flag)
        level = meta.get("data_level") or level

        name = (quote.name if quote else "") or meta.get("name") or symbol
        prev_close = meta.get("pre_close") or (quote.prev_close if quote else None)
        title = f"{name} · {label}（非逐笔）"
        if points:
            # 库里是 09:30–15:00 逐分钟，242 根（含 09:30 与 15:00 两个端点），
            # 而时间轴只有 241 个槽，直接写 242/241 会让人以为数据超了，所以超了只报点数
            title += (f" · {len(points)}/{TOTAL_SLOTS}" if len(points) <= TOTAL_SLOTS
                      else f" · {len(points)} 点")
            day = points[-1].time.date()
            if day != datetime.now().date():
                title += f" · {day:%m-%d}（最近交易日）"
        else:
            title += " · 暂无数据"

        self.single_view.set_intraday(
            points, prev_close,
            quote.limit_up_price if quote else None,
            quote.limit_down_price if quote else None,
            title, level, source="minute",
        )
        chart_day = points[-1].time.date() if points else None
        self.single_view.set_intraday_markers(
            [o for o in self.detector.orders(symbol, 200)
             if chart_day is not None and o.time.date() == chart_day])

    def _load_order_book(self, symbol: str) -> None:
        if not self.caps.order_book_levels or not hasattr(self.adapter, "get_order_book"):
            return
        self.jobs.submit("orderbook", self.adapter.get_order_book, symbol,
                         meta={"symbol": symbol})

    def _on_candle_scheme_changed(self, scheme: str) -> None:
        self._apply_candle_scheme(scheme)
        self.repo.set_setting(SETTING_KEYS["candle_scheme"], self.candle_scheme)
        tip = ("均线关系（红多头 / 绿空头 / 紫分歧）" if self.candle_scheme == "ma" else "涨跌")
        self.statusBar().showMessage(f"K线配色：{tip} · 已保存，重启保持", 4000)

    def _apply_candle_scheme(self, scheme: str) -> None:
        """一套配色喂给所有 K 线图：单股 / 强势回调 / 涨停四阶。"""
        self.candle_scheme = scheme if scheme in CANDLE_SCHEMES else "change"
        self.single_view.set_candle_scheme(self.candle_scheme)
        self.pullback_view.chart.set_candle_scheme(self.candle_scheme)
        self.limitup_view.chart.set_candle_scheme(self.candle_scheme)

    def _on_period_changed(self, period: str) -> None:
        if period not in PERIOD_LABELS:
            return
        self.chart_period = period
        self.single_view.set_period(period)
        self.repo.set_setting(SETTING_KEYS["period"], period)
        if period == "intraday" and self.intraday_poller is not None:
            self.intraday_poller.request_refresh()
        if self.current_symbol:
            self._load_chart(self.current_symbol)

    def _add_symbols(self, symbols: list[str], quiet: bool = False) -> None:
        if self.current_group_id is None:
            self.statusBar().showMessage("请先选择或新建一个分组", 4000)
            return
        added = self.repo.add_symbols(self.current_group_id, symbols)
        if not quiet:
            self.statusBar().showMessage(f"已加入 {added} 只（重复自动跳过）", 4000)
        self._refresh_groups(self.current_group_id, symbols[0] if symbols else None)

    def _on_remove_symbols(self, symbols: list[str]) -> None:
        if self.current_group_id is None:
            return
        removed = self.repo.remove_symbols(self.current_group_id, symbols)
        self.statusBar().showMessage(f"已移除 {removed} 只", 3000)
        self._refresh_groups(self.current_group_id)

    def _on_import(self) -> None:
        if self.current_group_id is None:
            QMessageBox.information(self, "提示", "请先选择或新建一个分组")
            return
        dialog = ImportSymbolsDialog(self)
        if dialog.exec() != dialog.Accepted:
            return
        tokens = dialog.tokens()
        resolved: list[str] = []
        unresolved: list[str] = []
        for token in tokens:
            symbol = self._resolve_token(token)
            if symbol:
                resolved.append(symbol)
            else:
                unresolved.append(token)
        if resolved:
            self._add_symbols(resolved)
        if unresolved:
            QMessageBox.warning(
                self, "部分未导入",
                "以下内容无法识别为股票代码或名称：\n" + "、".join(unresolved[:20]))

    def _resolve_token(self, token: str) -> str | None:
        text = token.strip().upper()
        if not text:
            return None
        if "." in text:
            return text if self.repo.get_stock(text) else None
        if text.isdigit() and len(text) == 6:
            for market in ("SH", "SZ", "BJ"):
                symbol = f"{text}.{market}"
                if self.repo.get_stock(symbol):
                    return symbol
            return None
        rows = self.repo.search(text, limit=1)
        return rows[0]["symbol"] if rows else None

    # ================================================================ 模式
    def _set_mode(self, mode: str) -> None:
        changed = mode != self.mode
        self.mode = mode
        self._apply_mode()
        if changed:                      # 同一模式反复点击（列表连点）不重复写库
            self.repo.set_setting(SETTING_KEYS["mode"], mode)
        if mode == "grid":
            self._refresh_grid()

    def _apply_mode(self) -> None:
        if self.mode not in ("single", "grid", "pullback", "limitup"):
            self.mode = "single"
        mode = self.mode
        self.btn_single.setChecked(mode == "single")
        self.btn_grid.setChecked(mode == "grid")
        self.btn_pullback.setChecked(mode == "pullback")
        self.btn_limitup.setChecked(mode == "limitup")
        self.grid_combo.setVisible(mode == "grid")
        # 盘口/成交/预警都是「当前这一只」的口径，选股页用不上，收起来把宽度让给候选表
        self.right_panel.setVisible(mode not in ("pullback", "limitup"))
        widget = {"grid": self.grid_view, "pullback": self.pullback_view,
                  "limitup": self.limitup_view}.get(mode, self.single_view)
        self.center.setCurrentWidget(widget)
        if mode == "grid":
            self.grid_view.set_size(self.grid_size)

    def _on_grid_size_changed(self) -> None:
        self.grid_size = int(self.grid_combo.currentData())
        self.grid_view.set_size(self.grid_size)
        self.repo.set_setting(SETTING_KEYS["grid_size"], self.grid_size)
        if self.mode == "grid":
            self._refresh_grid()

    def _refresh_grid(self) -> None:
        symbols = self.left.table.symbols()
        names = self.repo.stock_names(symbols)
        picked = [s for s in self.grid_symbols if s in symbols]
        if len(picked) < self.grid_size:
            picked += [s for s in symbols if s not in picked]
        picked = picked[: self.grid_size]
        self.grid_symbols = picked
        self.grid_view.set_symbols(picked, names)
        for symbol in picked:
            row = self.repo.cached_quotes([symbol]).get(symbol)
            if row:
                self.grid_view.update_quote(_quote_from_row(row))
            self._update_grid_series(symbol)

    def _update_grid_series(self, symbol: str) -> None:
        if self.mode != "grid":
            return
        cached = self.repo.cached_quotes([symbol]).get(symbol)
        prev_close = cached["prev_close"] if cached else None
        source = "em" if self.minute_feed.has_data(symbol) else "sampled"
        self.grid_view.update_series(symbol, self._intraday_series(symbol) or self.sampler.series(symbol),
                                     prev_close, "B", source)

    # ================================================================ 预警
    def _refresh_alert_rules(self) -> None:
        rules = self.repo.rules(enabled_only=False)
        if self.current_symbol:
            rules = [r for r in rules
                     if not r.get("symbols") or self.current_symbol in r["symbols"]]
        self.alert_panel.set_rules(rules)

    def _on_add_alert(self) -> None:
        if not self.current_symbol:
            self.statusBar().showMessage("先选一只股票再新增预警", 4000)
            return
        name = self.single_view.info.name.text()
        dialog = AlertRuleDialog(self.current_symbol, name, self)
        if dialog.exec() != dialog.Accepted:
            return
        payload = dialog.payload()
        rule_type = payload["rule_type"]
        if rule_type in ("volume_ratio_above", "turnover_rate_above"):
            self.alert_service.create_metrics_rule(
                self.current_symbol, rule_type, payload["value"],
                cooldown_sec=payload["cooldown_sec"])
        elif rule_type in INTRADAY_RULES_UI:
            self.alert_service.create_intraday_rule(
                self.current_symbol, rule_type, payload["value"],
                cooldown_sec=payload["cooldown_sec"])
        elif rule_type in AMOUNT_RULES:
            self.alert_service.create_large_order_rule(
                self.current_symbol, rule_type, payload["value"],
                cooldown_sec=payload["cooldown_sec"])
        else:
            self.alert_service.create_price_rule(
                self.current_symbol, rule_type, payload["value"],
                level=payload["level"], cooldown_sec=payload["cooldown_sec"])
        self.alert_service.reload()
        self._refresh_alert_rules()
        self.statusBar().showMessage("预警已保存并立即生效", 4000)

    def _on_delete_alert(self, rule_id: int) -> None:
        self.repo.delete_rule(rule_id)
        self.alert_service.reload()
        self._refresh_alert_rules()

    def _on_toggle_alert(self, rule_id: int, enabled: bool) -> None:
        self.repo.set_rule_enabled(rule_id, enabled)
        self.alert_service.reload()
        self._refresh_alert_rules()

    def _on_note_saved(self, text: str) -> None:
        if not text:
            self.statusBar().showMessage("笔记为空，未保存", 3000)
            return
        self.repo.add_note(self.current_symbol or None, text)
        self.logs.note.clear()
        self.statusBar().showMessage("笔记已写入 review_notes", 4000)

    # ================================================================ 定时刷新
    def _on_ui_tick(self) -> None:
        for result in self.poller.drain():
            self._apply_poll(result)
        if self.intraday_poller is not None:
            for update in self.intraday_poller.drain():
                self._apply_intraday(update)
        for done in self.jobs.poll():
            self._apply_job(done)

    def _apply_poll(self, result) -> None:
        self._poll_count += 1
        for quote in result.quotes:
            self.left.table.update_quote(quote)
            self.grid_view.update_quote(quote)
            if quote.symbol == self.current_symbol:
                self.single_view.set_quote(quote)
            self._update_grid_series(quote.symbol)

        # 采样成交与区间大单只上屏当前股票，避免右栏被其他标的刷掉
        if self.current_symbol:
            new_points = [p for p in result.points if p.symbol == self.current_symbol]
            if self.minute_feed.has_data(self.current_symbol):
                # 分钟线在时，采样点不能混进同一张表：两张表的区间含义不同（3s vs 60s），
                # 混着看会把 3 秒的量和 1 分钟的量当同一口径比较。
                new_points = []
            for point in new_points:
                self.trade_panel.append_point(point)
            new_orders = [o for o in result.orders if o.symbol == self.current_symbol]
            for order in new_orders:
                self.trade_panel.append_order(order)
                self.logs.append_event(format_large_order(
                    order, self.repo.stock_names([order.symbol]).get(order.symbol, "")))
            if new_orders:
                today = datetime.now().date()
                self.single_view.set_intraday_markers(
                    [o for o in self.detector.orders(self.current_symbol, 200)
                     if o.time.date() == today])
            if new_points and self.chart_period == "intraday":
                self._refresh_intraday(self.current_symbol)

        trading = self.clock.status().is_trading
        latency = self.poller.last_latency_ms or 0
        stale = latency > self.latency_abort_ms
        if stale:
            if not getattr(self, "_stale_warned", False):
                self._stale_warned = True
                self.statusBar().showMessage(
                    f"行情耗时 {latency}ms 超过 {self.latency_abort_ms}ms，"
                    f"暂停信号判定，仅刷新价格", 8000)
                self.logs.append_event(
                    f"行情延迟 {latency}ms 超过阈值，预警与异动判定暂停，恢复后自动继续")
        elif self.poller.fail_streak < 3:
            self._stale_warned = False
            names = self.repo.stock_names([q.symbol for q in result.quotes])
            fired = self.alert_service.evaluate(result.quotes, trading=trading)
            fired += self.alert_service.evaluate_large_orders(
                result.orders, names=names, trading=trading)
            # 序列来源必须与图上一致：东财分钟线在时不能用采样序列去判定区间成交额
            series = {s: self._intraday_series(s)[-40:] for s in self.poller.subscribed}
            fired += self.alert_service.evaluate_intraday(
                series, trading=trading, names=names)
            for item in fired:
                self.logs.append_alert(item.message)
                self.statusBar().showMessage(item.message, 8000)
                if item.event.level in ("P1", "P2"):
                    QApplication.beep()

        if self._poll_count % 4 == 0 and self.current_symbol:
            self._load_order_book(self.current_symbol)

        if not result.ok and self.poller.fail_streak >= 3:
            self.statusBar().showMessage(
                f"数据源无返回（连续 {self.poller.fail_streak} 次）：{self.poller.last_error}", 8000)

    def _apply_intraday(self, update) -> None:
        """东财分钟线到达：更新右栏、大单、标记与分时图。"""
        symbol = update.symbol
        is_focus = symbol == self.current_symbol

        if not update.ok:
            if is_focus and update.error:
                and_streak = self.intraday_poller.fail_streak if self.intraday_poller else 0
                if and_streak >= 3 and not getattr(self, "_intraday_down", False):
                    self._intraday_down = True
                    self.statusBar().showMessage(
                        f"分时取数失败（连续 {and_streak} 次）：{update.error}", 9000)
                    self.logs.append_event(
                        f"东方财富分时不可用（连续 {and_streak} 次）：{update.error}；"
                        f"分时图保留库内最近一个东财交易日，不回退其他数据源")
                # 分时图只认东财：这里刷新是为了显示「库内最近交易日」或明确的空状态
                if self.chart_period == "intraday" and not self.minute_feed.has_data(symbol):
                    self._refresh_intraday(symbol)
            return
        if getattr(self, "_intraday_down", False):
            self._intraday_down = False
            self.logs.append_event("东方财富分时已恢复")

        chart_day = update.series[-1].time.date() if update.series else datetime.now().date()

        if update.backfill_orders and is_focus:
            # 首载回放：历史分钟算出来的大单只上表、只画标记，不进日志也不弹告警
            self.trade_panel.set_orders(self.detector.orders(symbol, 200))
            self.single_view.set_intraday_markers(
                [o for o in self.detector.orders(symbol, 200) if o.time.date() == chart_day])

        if update.fresh:
            for point in update.fresh:
                if is_focus:
                    self.trade_panel.append_point(point)
            name = update.name or self.repo.stock_names([symbol]).get(symbol, "")
            for order in update.orders:
                if is_focus:
                    self.trade_panel.append_order(order)
                self.logs.append_event(format_large_order(order, name))
            if update.orders and is_focus:
                self.single_view.set_intraday_markers(
                    [o for o in self.detector.orders(symbol, 200) if o.time.date() == chart_day])
            if update.orders:
                # 分钟线产生的区间大单也要走预警判定，否则东财口径下大单永远不告警
                for item in self.alert_service.evaluate_large_orders(
                        update.orders, names={symbol: name},
                        trading=self.clock.status().is_trading):
                    self.logs.append_alert(item.message)
                    self.statusBar().showMessage(item.message, 8000)
                    if item.event.level in ("P1", "P2"):
                        QApplication.beep()

        if is_focus:
            if self._trade_live_symbol != symbol:
                # 该股第一次拿到分钟线：整日重铺。此前表里可能是库内还原的点（方向不明）
                # 或根本是空表，必须用线上整日序列覆盖，否则方向/均价与图上不一致。
                self._trade_live_symbol = symbol
                self._trade_loaded_symbol = symbol
                self.trade_panel.set_source_mode("em")
                self.trade_panel.set_points(update.series)
            if self.chart_period == "intraday":
                self._refresh_intraday(symbol)
                # 分钟线到达说明这一分钟的资金流也定型了，顺带刷新副图（内部按 15s 节流）
                self._load_intraday_money_flow(symbol)

        if self.mode == "grid":
            self._update_grid_series(symbol)

    def _apply_job(self, done) -> None:
        if done.error:
            if done.tag == "kline" and (done.meta or {}).get("symbol") == self.current_symbol:
                self.single_view.show_error(f"K线加载失败：{done.error}")
            elif done.tag == "iflow":
                # 取数器本身已把 DataSourceError 转成 (None, 原因)，走到这里说明是
                # 意料之外的异常：副图不能一直停在「加载中」，要把原文交出去
                self.single_view.clear_intraday_money_flow(f"分时资金流取数失败：{done.error}")
            else:
                self.statusBar().showMessage(f"{done.tag} 失败：{done.error}", 6000)
            return

        if done.tag == "kline":
            meta = done.meta or {}
            symbol = meta.get("symbol")
            period = meta.get("period")
            if symbol != self.current_symbol or period != self.chart_period:
                return
            bars = done.result or []
            if bars:
                self.repo.upsert_bars(bars)
                if period == "1d":
                    self.detector.set_daily_range_from_bars(
                        symbol, bars, self.detector.lookback_days)
            label = PERIOD_LABELS.get(period, period)
            self.single_view.set_bars(bars, label, self.caps.data_level, "exchange")
            self.single_view.set_event_marks(
                self.repo.large_orders_by_date(symbol, 120))
        elif done.tag == "moneyflow":
            meta = done.meta or {}
            if (meta.get("symbol") != self.current_symbol
                    or self.chart_period != "1d"):
                return
            rows, reason = done.result or ([], "")
            if reason or not rows:
                # 失败就松开标记，下次切回日K会重试（引擎刚启动的情况很常见）
                self._money_flow_symbol = ""
                self.single_view.clear_money_flow(reason or "无数据：该股没有资金流记录")
                return
            self.single_view.set_money_flow(
                rows, "stockdb 资金流", self.money_flow_adapter.data_level)
        elif done.tag == "iflow":
            meta = done.meta or {}
            if meta.get("symbol") != self.current_symbol or self.chart_period != "intraday":
                return
            series, reason = done.result or (None, "")
            if reason or series is None or not getattr(series, "points", None):
                self.single_view.clear_intraday_money_flow(
                    reason or "无数据：该股没有分钟资金流")
                if reason and reason != self._iflow_error:
                    # 失败不让副图空着：把原因原文写进格子，状态栏只在「新错误」时提一句，
                    # 否则断网期间每 15 秒弹一次同样的话，反而盖掉别的提示
                    self._iflow_error = reason
                    self.statusBar().showMessage(f"分时资金流取数失败：{reason}", 8000)
                return
            self._iflow_error = ""
            self.single_view.set_intraday_money_flow(
                series.points, "东方财富分时资金流", series.data_level)
        elif done.tag == "orderbook":
            meta = done.meta or {}
            if meta.get("symbol") != self.current_symbol:
                return
            cached = self.repo.cached_quotes([self.current_symbol]).get(self.current_symbol)
            self.order_book.set_order_book(done.result, cached["prev_close"] if cached else None)
        elif done.tag == "pullback-kline":
            symbol = (done.meta or {}).get("symbol")
            if symbol:
                self.pullback_view.set_chart_bars(symbol, done.result or [])
        elif done.tag == "limitup-kline":
            symbol = (done.meta or {}).get("symbol")
            if symbol:
                self.limitup_view.set_chart_bars(symbol, done.result or [])

    def _refresh_status(self) -> None:
        now = datetime.now()
        self.lbl_clock.setText(now.strftime("%H:%M:%S"))
        status = self.clock.status(now)
        self.lbl_market.setText(f"{status.label}")
        self.lbl_market.setStyleSheet(f"color:{COLORS[status.color_key]};")
        self.lbl_market.setToolTip(f"{status.detail}（日历：{'数据源' if status.calendar_source == 'api' else '本地近似'}）")

        pool = self.repo.stock_count()
        self.lbl_pool.setText(f"股票池 {pool} 只" if pool else "股票池 加载中…")

        connected = self.poller.fail_streak < 3
        latency = self.poller.last_latency_ms
        dot = "●"
        color = COLORS["up"] if connected else COLORS["down"]
        if self.caps.data_level in ("B", "C", "D"):
            color = COLORS["warn"]
        text = (f"{dot} {self.caps.data_level}级 {self.caps.note.split('：')[0]}"
                if self.caps.note else f"{dot} {self.caps.data_level}级")
        if latency is not None:
            text += f" · {latency}ms"
        if self.poller.fail_streak:
            text += f" · 失败{self.poller.fail_streak}次"
        self.lbl_source.setText(f"数据源：{text}")
        self.lbl_source.setStyleSheet(f"color:{color};")

        if pool > 0 and not self._universe_ready:
            self._universe_ready = True
            self.left.search.set_universe(self.universe)
            self.universe.build_pinyin_async()

        if self.mode == "grid" and self.grid_symbols and self._poll_count % 2 == 0:
            for symbol in self.grid_symbols:
                self._update_grid_series(symbol)

    def _on_manual_refresh(self) -> None:
        self.poller.request_refresh()
        if self.current_symbol:
            # F5 是「我现在就要最新」：绕过分时资金流的 15 秒节流
            self._load_chart(self.current_symbol, force=True)
        self._refresh_alert_rules()
        self.statusBar().showMessage("已请求刷新", 2000)

    # ================================================================ 杂项
    def _open_lhb(self) -> None:
        """龙虎榜窗口：数据来自东方财富公开披露，与行情源分开标注。"""
        if self.lhb_adapter is None:
            QMessageBox.information(self, "龙虎榜", self._lhb_error or "龙虎榜数据源未启用。")
            return
        from app.ui.lhb_dialog import LhbDialog     # 局部导入：不打开就不加载这个窗口
        if self._lhb_dialog is None:
            dialog = LhbDialog(self.lhb_adapter, self.repo, self.current_symbol, self)
            dialog.watch_requested.connect(self._on_lhb_watch)
            dialog.symbol_activated.connect(lambda s: self.open_symbol(s, source="龙虎榜"))
            dialog.data_changed.connect(self._on_lhb_data_changed)
            self._lhb_dialog = dialog
        dialog = self._lhb_dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def _on_lhb_watch(self, symbols: list[str]) -> None:
        if not symbols:
            return
        groups = self.repo.groups()
        target = next((g for g in groups if "龙虎榜" in (g["name"] or "")), None)
        group_id = target["id"] if target else self.repo.create_group("龙虎榜观察池")
        added = self.repo.add_symbols(group_id, symbols)
        self._refresh_groups(select_group=group_id)
        names = self.repo.stock_names(symbols)
        label = "、".join(f"{names.get(s, s)}" for s in symbols)
        self.statusBar().showMessage(f"已加入龙虎榜观察池：{label}（新增 {added}）", 5000)
        self.logs.append_event(f"龙虎榜：{label} 加入龙虎榜观察池（新增 {added} 只）")

    def _on_lhb_data_changed(self) -> None:
        """窗口里抓到新披露数据后，刷新当前股票的 K 线标记。"""
        if self.current_symbol:
            self._load_lhb_marks(self.current_symbol)

    # ---------------------------------------------------------- 强势回调选股
    def _on_pullback_symbol(self, symbol: str) -> None:
        """候选表双击 / 「打开」按钮 → 切回单股精盯，这样能直接看分时/盘口/成交。"""
        self.open_symbol(symbol, source="强势回调")

    def _on_pullback_add(self, symbols: list[str]) -> None:
        if not symbols:
            return
        groups = self.repo.groups()
        target = next((g for g in groups if "强势回调" in (g["name"] or "")), None)
        group_id = target["id"] if target else self.repo.create_group("强势回调池")
        added = self.repo.add_symbols(group_id, symbols)
        self._refresh_groups(select_group=group_id)
        names = self.repo.stock_names(symbols)
        label = "、".join(names.get(s, s) for s in symbols)
        self.statusBar().showMessage(f"已加入强势回调池：{label}（新增 {added}）", 5000)
        self.logs.append_event(f"强势回调：{label} 加入强势回调池（新增 {added} 只）")

    def _load_pullback_bars(self, symbol: str) -> None:
        """恢复上次扫描结果时，内存里没有日线，按需向行情源要一份（不影响选股结论）。"""
        if not symbol:
            return
        self.jobs.submit("pullback-kline", self.adapter.get_kline, symbol, "1d",
                         None, None, 220, meta={"symbol": symbol})

    def _on_pullback_message(self, text: str) -> None:
        if not text:
            return
        self.statusBar().showMessage(text, 8000)
        self.logs.append_event(text)

    # ---------------------------------------------------------- 涨停四阶段
    def _on_limitup_symbol(self, symbol: str) -> None:
        """候选表双击 / 「打开」按钮 → 切回单股精盯，看分时/盘口/成交。"""
        self.open_symbol(symbol, source="涨停四阶")

    def _on_limitup_add(self, symbols: list[str]) -> None:
        if not symbols:
            return
        groups = self.repo.groups()
        target = next((g for g in groups if "涨停四阶段" in (g["name"] or "")), None)
        group_id = target["id"] if target else self.repo.create_group("涨停四阶段池")
        added = self.repo.add_symbols(group_id, symbols)
        self._refresh_groups(select_group=group_id)
        names = self.repo.stock_names(symbols)
        label = "、".join(names.get(s, s) for s in symbols)
        self.statusBar().showMessage(f"已加入涨停四阶段池：{label}（新增 {added}）", 5000)
        self.logs.append_event(f"涨停四阶段：{label} 加入涨停四阶段池（新增 {added} 只）")

    def _load_limitup_bars(self, symbol: str) -> None:
        """恢复上次扫描结果时按需回取日K（不改变选股结论）。"""
        if not symbol:
            return
        self.jobs.submit("limitup-kline", self.adapter.get_kline, symbol, "1d",
                         None, None, 220, meta={"symbol": symbol})

    def _on_limitup_message(self, text: str) -> None:
        if not text:
            return
        self.statusBar().showMessage(text, 8000)
        self.logs.append_event(text)

    def _load_lhb_marks(self, symbol: str) -> None:
        marks = self.repo.lhb_marks(symbol, 400) if symbol else {}
        self.single_view.set_lhb_marks(marks)
        if not marks:
            return
        latest = max(marks)
        info = marks[latest]
        reason = info.get("reason") or "原因未注明"
        self.logs.append_event(
            f"龙虎榜：{symbol} 最近上榜 {latest}（{reason}），"
            f"净买入 {info['net'] / 1e4:.0f}万 · C 级披露数据")

    def _on_backup(self) -> None:
        path = self.db.backup()
        QMessageBox.information(self, "备份完成", f"数据库已备份到：\n{path}")

    def _show_caps(self) -> None:
        caps = self.caps
        QMessageBox.information(
            self, "数据源能力",
            f"数据源：{self.config.get('datasource', {}).get('name', 'unknown')}\n"
            f"等级：{caps.data_level}\n"
            f"实时：{'是' if caps.realtime else '否'}\n"
            f"盘口：{caps.order_book_levels} 档\n"
            f"逐笔：{'支持' if caps.tick else '不支持（界面不渲染空壳）'}\n"
            f"分钟K：{'支持' if caps.minute_kline else '不支持（分时用快照采样替代）'}\n"
            f"龙虎榜：{'东方财富公开披露（C 级 · T 日盘后）' if self.lhb_adapter else '未启用（配置 lhb.enabled）'}\n"
            f"说明：{caps.note}")


def format_large_order(order: LargeOrder, name: str = "") -> str:
    """异动记录文案：明确写「区间累计」，不写单笔，也不写身份结论。"""
    label = name or order.symbol
    return (f"{label} {order.tier_label} · 区间累计 {order.amount / 1e4:.0f}万 · "
            f"{order.direction_label} · {order.position_label} · {order.tag}"
            f"（{order.price:.2f}，{order.time.strftime('%H:%M:%S')}）")


def _order_from_row(row: dict) -> LargeOrder:
    """把 large_order_events 行还原成事件对象，用于重启后回显。"""
    try:
        moment = datetime.fromisoformat(row["event_time"])
    except (ValueError, KeyError):
        moment = datetime.now()
    return LargeOrder(
        symbol=row["symbol"], time=moment, tier=row["tier"],
        direction=row["direction"], price=float(row["price"] or 0.0),
        volume=int(row["volume"] or 0), amount=float(row["amount"] or 0.0),
        threshold=float(row["threshold"] or 0.0),
        price_position=row.get("price_position") or "unknown",
        tag=row.get("tag") or "资金行为不明确",
        data_level=row.get("data_level") or "B",
        source=row.get("source") or "tqcenter:sampled",
    )


def _quote_from_row(row: dict, name: str = "") -> Quote:
    """把 quotes_cache 行还原成 Quote，用于冷启动/切组时立即显示。"""
    def parse(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    return Quote(
        symbol=row["symbol"], name=name,
        price=row.get("price"), prev_close=row.get("prev_close"),
        open=row.get("open"), high=row.get("high"), low=row.get("low"),
        volume=row.get("volume"), amount=row.get("amount"),
        turnover_rate=row.get("turnover_rate"), volume_ratio=row.get("volume_ratio"),
        limit_up_price=row.get("limit_up_price"), limit_down_price=row.get("limit_down_price"),
        data_level=row.get("data_level") or "C", source=row.get("source") or "",
        quote_time=parse(row.get("quote_time")), received_at=parse(row.get("received_at")),
    )
