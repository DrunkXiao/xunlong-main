"""图表组件：日K/周K/月K 蜡烛图与采样分时图（纯 QPainter，无外部图表依赖）。

约定：
- 红涨绿跌，空心上影红、实心下跌绿（A 股习惯）；
- 每张图右上角必须显示周期与数据等级，采样图额外标「采样」；
- 无数据时居中写「无数据」并说明原因，不画假曲线。
"""
from __future__ import annotations

import math
from datetime import datetime, time

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from app.db.models import Bar
from app.services.intraday import SamplePoint
from app.ui.theme import COLORS, MONO_FAMILY, UI_FAMILY, color_for_change

UP = QColor(COLORS["up"])
DOWN = QColor(COLORS["down"])
TEXT = QColor(COLORS["text"])
DIM = QColor(COLORS["text_dim"])
MUTED = QColor(COLORS["text_muted"])
GRID = QColor(COLORS["border"])
WARN = QColor(COLORS["warn"])
INFO = QColor(COLORS["info"])
PANEL = QColor(COLORS["panel"])

MA_COLORS = ["#ffee00", "#FF0000", "#00c40a", "#FF00D4"]

# K 线配色方案：涨跌（默认，红涨绿跌） / 均线关系（按收盘价与 MA5/MA20 的排列上色）
CANDLE_SCHEMES = {"change": "涨跌配色", "ma": "均线关系"}
CANDLE_SCHEME_TIP = (
    "涨跌配色：收盘 ≥ 开盘 红，否则绿。\n"
    "均线关系：收 > MA5 且 MA5 > MA20 → 红（多头排列）；\n"
    "　　　　　收 < MA5 且 MA5 < MA20 → 绿（空头排列）；\n"
    "　　　　　收与 MA5、MA5 与 MA20 方向不一致 → 紫（分歧）；\n"
    "　　　　　等于均线（贴线）按分歧处理；MA20 未成形的早期 K 线退回涨跌配色。")
# 分歧色用紫色：既不是多头红也不是空头绿，避免把「价在均线上但均线仍空头」读成看多
CANDLE_PURPLE = "#ae00ff"


def candle_color(close: float, open_: float, ma5: float | None,
                 ma20: float | None, scheme: str = "change") -> str:
    """单根 K 线颜色（返回十六进制，便于单测直接断言）。

    `ma` 方案严格按四条规则判定；相等（收=MA5 或 MA5=MA20）不属于任何一条，
    归入「分歧」紫——不能因为取等就悄悄算成多头。MA 未成形的早期 K 线退回涨跌色。
    """
    if scheme != "ma" or ma5 is None or ma20 is None:
        return COLORS["up"] if close >= open_ else COLORS["down"]
    if close > ma5 and ma5 > ma20:
        return COLORS["up"]
    if close < ma5 and ma5 < ma20:
        return COLORS["down"]
    return CANDLE_PURPLE

# 分时资金流五条曲线：(字段, 图例名, 颜色)。颜色照参考图：主力黄、超大红、
# 大单品红、中单浅蓝、小单绿。这五种色与涨跌红绿刻意错开——资金流是「按单笔
# 金额分档的净额」，不是涨跌方向，撞色会让人把「超大单红线」读成「股价上涨」。
FLOW_SERIES = (
    ("main", "主力", "#FFE000"),
    ("jumbo", "超大单", "#FF3B30"),
    ("big", "大单", "#FF00CC"),
    ("mid", "中单", "#4FC3F7"),
    ("small", "小单", "#3FD45A"),
)
FLOW_SHORT_LABEL = {"超大单": "超大"}

# 分时时间轴：09:30-11:30(121) + 13:00-15:00(120)，共 241 个分钟槽
TOTAL_SLOTS = 241
MARKER_TIERS = ("super", "continuous")


def is_minute_source(source: str) -> bool:
    """这条序列是不是「真分钟线」。

    来源标记有三处会用到（东财 `em`、free-stockdb `minute`、快照采样 `sampled`）。
    图例、空状态文案、时间轴门槛都只关心「分钟线 vs 采样」这一个区别，
    所以统一走这个判定，避免换分时数据源时到处漏改 `== "em"`。
    """
    return (source or "").lower() in ("em", "minute", "stockdb", "eastmoney")


def _fmt_yuan(value) -> str:
    """图表内的金额简写：万/亿；None 显示 --，不写 0。"""
    if value is None:
        return "--"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.2f}亿"
    if abs(amount) >= 1e4:
        return f"{amount / 1e4:.2f}万"
    return f"{amount:.0f}"


def _fmt_signed(value) -> str:
    """带正号的金额简写：净流入要能一眼看出「+」，0 不写符号。"""
    text = _fmt_yuan(value)
    try:
        return f"+{text}" if float(value) > 0 else text
    except (TypeError, ValueError):
        return text


def _cumulative(values: list[float | None]) -> list[float | None]:
    """把逐日净额累加成累计净额；首日之前是 None，缺日沿用前值（线不断开）。

    缺日不给 0 也不断开：断线会被读成「累计归零重来」，给 0 等于凭空造一天
    「没有资金进出」。沿用前值只表示「这一天没算进累计」，口径最保守。
    """
    out: list[float | None] = []
    total = 0.0
    started = False
    for value in values:
        if value is None:
            out.append(total if started else None)
            continue
        total += float(value)
        started = True
        out.append(total)
    return out


def _fmt_compact_yuan(value) -> str:
    """坐标轴/图例的紧凑金额：亿留两位、万取整。万级不再写 `.00`，否则刻度挤满。

    负数带 `-`，None 写 `--`（不写 0：资金流里 0 是「真的持平」，缺数是两回事）。
    """
    if value is None:
        return "--"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(amount) >= 1e8:
        return f"{amount / 1e8:.2f}亿"
    if abs(amount) >= 1e4:
        return f"{amount / 1e4:.0f}万"
    return f"{amount:.0f}"


def _fmt_compact_signed(value) -> str:
    """净额的紧凑写法：正数补 `+`，让「流入 / 流出」一眼可辨。"""
    text = _fmt_compact_yuan(value)
    try:
        return f"+{text}" if float(value) > 0 else text
    except (TypeError, ValueError):
        return text


def _nice_step(span: float, target: int = 4) -> float:
    """把任意跨度收敛到 1/2/2.5/5×10^n 的刻度步长，读起来是整数亿/万。"""
    if span <= 0:
        return 1.0
    raw = span / max(1, target)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10.0 * magnitude


def intraday_slot(moment: datetime) -> int:
    """把时间映射到 0..240 的分钟槽，多股共享时间轴与单股分时用同一套坐标。"""
    t = moment.time()
    minutes = (t.hour - 9) * 60 + t.minute - 30
    if t >= time(13, 0):
        minutes -= 90
    elif t > time(11, 30):
        minutes = 120
    return max(0, min(TOTAL_SLOTS - 1, minutes))


def _ma(closes: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    total = 0.0
    for idx, value in enumerate(closes):
        total += value
        if idx >= window:
            total -= closes[idx - window]
        out.append(total / window if idx >= window - 1 else None)
    return out


class _ChartBase(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(180)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self._hover: QPointF | None = None
        self.empty_reason = "无数据"

    # ------------------------------------------------------------ 绘制辅助
    def _draw_empty(self, painter: QPainter, reason: str | None = None) -> None:
        painter.setPen(QPen(MUTED))
        font = QFont(UI_FAMILY, 10)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, reason or self.empty_reason)

    def _draw_badge(self, painter: QPainter, text: str, color: QColor = MUTED) -> None:
        # 先定字体再量宽度：调用方可能刚设过别的字号，用错字体量出来的框会窄一截，
        # 中文被左右各切掉半个字（分时资金流的「1分钟 · 累计 · B级」就踩过）
        painter.setFont(QFont(MONO_FAMILY, 8))
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(text) + 12
        rect = QRectF(self.width() - width - 8, 4, width, 15)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(COLORS["raised"]))
        painter.drawRoundedRect(rect, 3, 3)
        painter.setPen(QPen(color))
        painter.drawText(rect, Qt.AlignCenter, text)

    # ------------------------------------------------------------ 布局计算
    def _plot_rect(self) -> QRectF:
        right = 64
        bottom = 18
        top = 30
        vol_h = max(46, (self.height() - top - bottom) * 0.22)
        return QRectF(8, top, max(10, self.width() - right - 8),
                      max(20, self.height() - top - bottom - vol_h - 6))

    def _vol_rect(self) -> QRectF:
        plot = self._plot_rect()
        bottom = 18
        return QRectF(plot.left(), plot.bottom() + 6, plot.width(),
                      max(20, self.height() - plot.bottom() - bottom - 6))

    def _grid_pen(self) -> QPen:
        pen = QPen(GRID)
        pen.setWidthF(1.0)
        pen.setStyle(Qt.DotLine)
        return pen

    def _axis_font(self) -> QFont:
        return QFont(MONO_FAMILY, 8)

    def enterEvent(self, event) -> None:  # noqa: N802
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self.update()


class CandleChart(_ChartBase):
    """K 线主图 + 成交量副图 + 十字光标。"""

    hover_changed = Signal(object)   # dict | None
    # 可视区间变了（换股 / 滚轮缩放 / 拖动）：下方资金流副图靠它跟着重画，
    # 否则主图缩到 20 根、副图还按 100 根画，柱子会比蜡烛多出一截。
    view_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.bars: list[Bar] = []
        self.period_label = "日K"
        self.data_level = "C"
        self.quality = "exchange"
        self.visible_count = 100
        self._offset = 0            # 0 表示最右（最新）
        self._ma_cache: dict[int, list[float | None]] = {}
        self._drag_x: float | None = None
        self.event_marks: dict[str, dict] = {}   # "YYYY-MM-DD" -> {count, max_tier, buy, sell}
        self.lhb_marks: dict[str, dict] = {}     # "YYYY-MM-DD" -> {count, reason, net, buy, sell}
        self.price_levels: list[dict] = []       # 参考价位：支撑/止损/目标/买入区间
        self.focus_date = None
        self.candle_scheme = "change"            # 涨跌配色 / 均线关系
        self.empty_reason = "无数据：尚未加载K线"

    # ------------------------------------------------------------ 配色
    def set_candle_scheme(self, scheme: str) -> None:
        """切换 K 线配色方案；未知值退回涨跌配色，不静默留空。"""
        self.candle_scheme = scheme if scheme in CANDLE_SCHEMES else "change"
        self.update()

    # ------------------------------------------------------------ 数据
    def set_bars(self, bars: list[Bar], period_label: str, data_level: str,
                 quality: str = "exchange") -> None:
        self.bars = list(bars)
        self.period_label = period_label
        self.data_level = data_level
        self.quality = quality
        closes = [b.close for b in self.bars]
        self._ma_cache = {w: _ma(closes, w) for w in (5, 10, 20, 60)}
        self._offset = 0
        self.visible_count = min(max(30, self.visible_count), max(30, len(self.bars)))
        self.empty_reason = "无数据：该周期未返回K线"
        self.update()
        self.view_changed.emit()

    def _slice(self) -> tuple[list[Bar], int]:
        n = len(self.bars)
        count = min(self.visible_count, n)
        end = n - self._offset
        start = max(0, end - count)
        return self.bars[start:end], start

    def visible_bars(self) -> list[Bar]:
        """当前真正画在屏幕上的那几根 K 线（供对齐副图用）。"""
        return self._slice()[0]

    def set_event_marks(self, marks: dict[str, dict]) -> None:
        """日K 上的大单事件标记，按交易日聚合。"""
        self.event_marks = dict(marks or {})
        self.update()

    def set_lhb_marks(self, marks: dict[str, dict]) -> None:
        """日K 上的龙虎榜标记（披露事实，不是推断）。"""
        self.lhb_marks = dict(marks or {})
        self.update()

    def set_focus_date(self, day) -> None:
        self.focus_date = day
        self.update()

    def set_price_levels(self, levels: list[dict] | None) -> None:
        """参考价位线（选股结果的支撑/止损/目标）。

        每一项：`{"price": 12.3, "label": "止损", "color": "#12B76A"}`，
        或用 `{"low": 12.1, "high": 12.5, ...}` 画一条半透明区间带。
        价位写成文字而不是画箭头：这些线是「计划」，不是成交事实。
        """
        self.price_levels = [dict(item) for item in (levels or [])]
        self.update()

    # ------------------------------------------------------------ 交互
    def wheelEvent(self, event) -> None:  # noqa: N802
        if not self.bars:
            return
        step = max(2, self.visible_count // 10)
        if event.angleDelta().y() > 0:
            self.visible_count = max(20, self.visible_count - step)
        else:
            self.visible_count = min(len(self.bars), self.visible_count + step)
        self.update()
        self.view_changed.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_x = event.position().x()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_x = None
        self.view_changed.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        moved = False
        if self._drag_x is not None and self.bars:
            dx = pos.x() - self._drag_x
            slot = max(1.0, (self.width() - 72) / max(1, self.visible_count))
            shift = int(dx // slot)
            if shift:
                before = self._offset
                self._offset = max(0, min(len(self.bars) - self.visible_count,
                                          self._offset + shift))
                self._drag_x = pos.x()
                moved = self._offset != before
        self._hover = pos
        self.update()
        if moved:
            self.view_changed.emit()
        self.hover_changed.emit(self._hover_payload(pos))

    def _hover_payload(self, pos: QPointF) -> dict | None:
        if not self.bars:
            return None
        visible, start = self._slice()
        rect = self._plot_rect()
        if not rect.contains(pos) or not visible:
            return None
        slot = rect.width() / len(visible)
        idx = int((pos.x() - rect.left()) / slot)
        idx = max(0, min(len(visible) - 1, idx))
        bar = visible[idx]
        return {
            "time": bar.bar_time.strftime("%Y-%m-%d"),
            "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close,
            "volume": bar.volume, "amount": bar.amount,
            "index": start + idx, "slot": slot, "left": rect.left(),
        }

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)
        if not self.bars:
            self._draw_empty(painter)
            return

        visible, start = self._slice()
        if not visible:
            self._draw_empty(painter)
            return

        plot = self._plot_rect()
        vol = self._vol_rect()
        highs = [b.high for b in visible]
        lows = [b.low for b in visible]
        for window in (5, 10, 20, 60):
            for value in self._ma_cache[window][start:start + len(visible)]:
                if value is not None:
                    highs.append(value)
                    lows.append(value)
        base_hi, base_lo = max(highs), min(lows)
        if base_hi <= base_lo:
            base_hi, base_lo = base_hi + 0.01, base_lo - 0.01
        pad = (base_hi - base_lo) * 0.06
        hi = base_hi + pad
        lo = base_lo - pad
        # 参考价位可以撑开坐标轴，但最多上下各 35%：止损/目标离收盘常常 ±40%，
        # 全量纳入会把一整段蜡烛压成一条横线，图就没法看了。超出的价位改为贴边标注。
        room = (hi - lo) * 0.12
        max_hi, min_lo = hi + room, lo - room
        for level in self.price_levels:
            for key in ("price", "low", "high"):
                try:
                    value = float(level.get(key))
                except (TypeError, ValueError):
                    continue
                hi = max(hi, min(value, max_hi))
                lo = min(lo, max(value, min_lo))
        span = hi - lo

        def y_of(price: float) -> float:
            return plot.bottom() - (price - lo) / span * plot.height()

        # 网格 + 价格刻度
        painter.setFont(self._axis_font())
        for i in range(5):
            price = lo + span * i / 4
            y = y_of(price)
            painter.setPen(self._grid_pen())
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(DIM))
            painter.drawText(QRectF(plot.right() + 4, y - 8, 58, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, f"{price:.2f}")

        slot = plot.width() / len(visible)
        body_w = max(1.5, slot * 0.68)

        # 蜡烛：颜色由配色方案决定（涨跌 / 均线关系），形态一律实心
        ma5_series = self._ma_cache.get(5) or []
        ma20_series = self._ma_cache.get(20) or []
        for idx, bar in enumerate(visible):
            cx = plot.left() + slot * (idx + 0.5)
            abs_idx = start + idx
            ma5 = ma5_series[abs_idx] if abs_idx < len(ma5_series) else None
            ma20 = ma20_series[abs_idx] if abs_idx < len(ma20_series) else None
            color = QColor(candle_color(bar.close, bar.open, ma5, ma20, self.candle_scheme))
            painter.setPen(QPen(color, 1.0))
            painter.drawLine(QPointF(cx, y_of(bar.high)), QPointF(cx, y_of(bar.low)))
            top = y_of(max(bar.open, bar.close))
            bottom = y_of(min(bar.open, bar.close))
            rect = QRectF(cx - body_w / 2, top, body_w, max(1.0, bottom - top))
            painter.setBrush(color)
            painter.setPen(QPen(color, 0.5))
            painter.drawRect(rect)

        # 大单事件标记：三角形落在当日最低价下方，颜色按买卖金额净差
        for idx, bar in enumerate(visible):
            mark = self.event_marks.get(bar.bar_time.strftime("%Y-%m-%d"))
            if not mark:
                continue
            cx = plot.left() + slot * (idx + 0.5)
            y = min(plot.bottom() - 4, y_of(bar.low) + 8)
            net = float(mark.get("buy", 0.0)) - float(mark.get("sell", 0.0))
            color = UP if net >= 0 else DOWN
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                QPointF(cx, y - 4), QPointF(cx - 3.6, y + 2.6), QPointF(cx + 3.6, y + 2.6)]))
            if slot >= 26:
                painter.setPen(QPen(color))
                painter.setFont(QFont(MONO_FAMILY, 7))
                painter.drawText(QRectF(cx - 14, y + 3, 28, 11), Qt.AlignCenter,
                                 str(mark.get("count", 0)))

        # 龙虎榜标记：官方披露事实，画在最高价上方，方形+「榜」字，与区间大单三角区分
        if self.lhb_marks and slot >= 4:
            for idx, bar in enumerate(visible):
                info = self.lhb_marks.get(bar.bar_time.strftime("%Y-%m-%d"))
                if not info:
                    continue
                cx = plot.left() + slot * (idx + 0.5)
                y = max(plot.top() + 6.0, y_of(bar.high) - 9.0)
                size = 6.0 if slot >= 6 else 3.6
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(COLORS["warn"]))
                painter.drawRect(QRectF(cx - size / 2, y - size / 2, size, size))
                if slot >= 14:
                    painter.setPen(QPen(QColor(COLORS["warn"])))
                    painter.setFont(QFont(MONO_FAMILY, 7))
                    painter.drawText(QRectF(cx - 12, y - size - 9, 24, 10), Qt.AlignCenter, "榜")

        if self.focus_date is not None:
            key = (self.focus_date.strftime("%Y-%m-%d")
                   if hasattr(self.focus_date, "strftime") else str(self.focus_date))
            for idx, bar in enumerate(visible):
                if bar.bar_time.strftime("%Y-%m-%d") == key:
                    cx = plot.left() + slot * (idx + 0.5)
                    painter.setPen(QPen(INFO, 1, Qt.DashLine))
                    painter.drawLine(QPointF(cx, plot.top()), QPointF(cx, vol.bottom()))
                    break

        # 均线
        legend_y = 10
        for i, window in enumerate((5, 10, 20, 60)):
            series = self._ma_cache[window][start:start + len(visible)]
            poly = QPolygonF([
                QPointF(plot.left() + slot * (idx + 0.5), y_of(value))
                for idx, value in enumerate(series) if value is not None
            ])
            painter.setPen(QPen(QColor(MA_COLORS[i]), 1.2))
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(poly)
            last = next((v for v in reversed(series) if v is not None), None)
            text = f"MA{window} {last:.2f}" if last else f"MA{window} --"
            painter.setPen(QPen(QColor(MA_COLORS[i])))
            painter.setFont(QFont(MONO_FAMILY, 8))
            painter.drawText(QRectF(plot.left() + 2 + i * 88, legend_y - 7, 86, 14),
                             Qt.AlignLeft | Qt.AlignVCenter, text)

        # 参考价位：支撑/止损/目标/买入区间（「计划」线，不对应任何成交）
        # 价位若落在视图外（比如 +60% 的目标价），不画线、只在边缘标「↑ 价」：
        # 画一条贴边的虚线会被读成「价格就在这儿」，那是假信息。
        placed: list[QRectF] = []
        for level in self.price_levels:
            color = QColor(level.get("color") or COLORS["text_dim"])
            label = str(level.get("label") or "")
            text = label
            try:
                if level.get("low") is not None and level.get("high") is not None:
                    y1 = y_of(float(level["low"]))
                    y2 = y_of(float(level["high"]))
                    top, bottom = min(y1, y2), max(y1, y2)
                    if bottom < plot.top() or top > plot.bottom():
                        continue
                    top = max(plot.top(), top)
                    bottom = min(plot.bottom(), bottom)
                    band = QColor(color)
                    band.setAlpha(30)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(band)
                    painter.drawRect(QRectF(plot.left(), top, plot.width(),
                                            max(1.0, bottom - top)))
                    anchor = top
                elif level.get("price") is not None:
                    price = float(level["price"])
                    raw_y = y_of(price)
                    anchor = min(max(raw_y, plot.top() + 5), plot.bottom() - 5)
                    pinned = abs(anchor - raw_y) > 0.5
                    if not pinned:
                        painter.setPen(QPen(color, 1.0, Qt.DashLine))
                        painter.setBrush(Qt.NoBrush)
                        painter.drawLine(QPointF(plot.left(), anchor),
                                         QPointF(plot.right(), anchor))
                    if label:
                        mark = ("↑" if raw_y < anchor else "↓") if pinned else ""
                        text = f"{mark}{label} {price:.2f}"
                else:
                    continue
            except (TypeError, ValueError):
                continue
            if not text:
                continue
            painter.setFont(QFont(MONO_FAMILY, 7))
            width = painter.fontMetrics().horizontalAdvance(text) + 8
            top_y = anchor - 11
            for _ in range(6):          # 止损与买入区间下沿常常挨在一起，标签逐级上移避让
                box = QRectF(plot.right() - width - 10, top_y, width, 10)
                if not any(box.intersects(other) for other in placed):
                    break
                top_y -= 11
            top_y = max(plot.top() + 1, top_y)
            box = QRectF(plot.right() - width - 10, top_y, width, 10)
            placed.append(box)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(COLORS["raised"]))
            painter.drawRoundedRect(box, 2, 2)
            painter.setPen(QPen(color))
            painter.drawText(box, Qt.AlignCenter, text)

        # 成交量
        max_vol = max((b.volume for b in visible), default=0) or 1
        painter.setPen(self._grid_pen())
        painter.drawLine(QPointF(vol.left(), vol.top()), QPointF(vol.right(), vol.top()))
        for idx, bar in enumerate(visible):
            cx = vol.left() + slot * (idx + 0.5)
            h = (bar.volume / max_vol) * (vol.height() - 2)
            color = UP if bar.close >= bar.open else DOWN
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRect(QRectF(cx - body_w / 2, vol.bottom() - h, body_w, max(0.8, h)))

        # 时间刻度
        painter.setPen(QPen(DIM))
        painter.setFont(self._axis_font())
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            idx = min(len(visible) - 1, int(frac * (len(visible) - 1)))
            bar = visible[idx]
            x = plot.left() + slot * (idx + 0.5)
            label = bar.bar_time.strftime("%y-%m-%d")
            left = min(max(plot.left(), x - 34), plot.right() - 68)
            painter.drawText(QRectF(left, vol.bottom() + 2, 68, 14),
                             Qt.AlignCenter, label)

        # 十字光标
        if self._hover and plot.contains(self._hover):
            payload = self._hover_payload(self._hover)
            if payload:
                x = payload["left"] + (payload["index"] - start + 0.5) * slot
                painter.setPen(QPen(QColor(COLORS["text_dim"]), 0.8, Qt.DashLine))
                painter.drawLine(QPointF(x, plot.top()), QPointF(x, vol.bottom()))
                painter.drawLine(QPointF(plot.left(), self._hover.y()),
                                 QPointF(plot.right(), self._hover.y()))
                bar = visible[payload["index"] - start]
                info = (f"{payload['time']}  开{bar.open:.2f} 高{bar.high:.2f} "
                        f"低{bar.low:.2f} 收{bar.close:.2f}")
                lhb = self.lhb_marks.get(payload["time"])
                if lhb:
                    info += f"   [龙虎榜 净买入 {_fmt_yuan(lhb.get('net'))}]"
                painter.setPen(QPen(TEXT))
                painter.setFont(QFont(MONO_FAMILY, 8))
                painter.drawText(QRectF(plot.left() + 2, plot.bottom() - 15, 420, 14),
                                 Qt.AlignLeft, info)

        badge = f"{self.period_label} · {self.data_level}级"
        if self.candle_scheme == "ma":
            badge += " · 均线配色"      # 颜色换了口径必须写出来，否则会被当成涨跌色
        self._draw_badge(painter, badge, WARN if self.quality == "sampled" else MUTED)


class MoneyFlowChart(_ChartBase):
    """日K 下方的「主力资金」副图：每日净额柱 + 两条累计净流入曲线。

    为什么是「柱 + 累计线」两张皮：
    柱子回答「今天主力是买还是卖」，累计线回答「这一段时间主力整体是进还是出」。
    只看柱子会被单日噪音带偏（一天的大买可能只是前面几天的回补），
    只看累计线又看不出是哪几天进的，所以两条都要，且共用一根 y 轴（都是元）。

    累计线为什么从可视区间左端起算：
    左端归零、右端就是屏幕这一段的累计，跟柱子对得上。若从全历史起算，
    一条早就累计到几十亿的线会把区间内的变化压成一条平线，等于没画。

    为什么不塞进 CandleChart：
    - 净额是金额（元），K 线是价格（元/股），共用一根 y 轴必然把其中一个压平；
    - 主图支持滚轮缩放和拖动，副图必须跟着**同一批** bar 走。这里每帧向主图要
      `visible_bars()`，与蜡烛共用同一个 slice，柱子不会比蜡烛多一根；
    - LEFT/RIGHT 与 `CandleChart._plot_rect()` 保持一致，柱心与蜡烛中心同一条竖线。

    口径：`main_net` = 超大单 + 大单，`jumbo_net` = 超大单，单位元，数据源直接给出，
    本图不做任何二次加工（除累计求和）。
    """

    LEFT = 8
    RIGHT = 64          # 与 CandleChart._plot_rect() 的 64 一致
    TITLE_H = 17
    AXIS_H = 15
    # 两条累计线用黄/粉：既不与涨跌红绿撞色，也能在纯黑底上拉开对比
    CUM_MAIN_COLOR = "#E8D24A"
    CUM_JUMBO_COLOR = "#FF6B81"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedHeight(150)     # 标题行 + 累计线 + 柱 + 日期轴，再矮就挤在一起
        self._flow: dict[str, float] = {}
        self._jumbo: dict[str, float] = {}
        self._candles: CandleChart | None = None
        self._hover_key = ""
        self.empty_reason = "无数据：资金流尚未返回"
        self.source_label = ""
        self.data_level = "C"

    # ------------------------------------------------------------ 数据
    def bind(self, candles: CandleChart) -> None:
        """绑定主图：主图缩放/拖动/换股时副图跟着重画。"""
        self._candles = candles
        candles.view_changed.connect(self.update)
        candles.hover_changed.connect(self._on_hover)

    def set_flow(self, flow: dict[str, float], source_label: str = "",
                 data_level: str = "", jumbo: dict[str, float] | None = None) -> None:
        """写入 `{"YYYY-MM-DD": 净额(元)}`：主力净额必给，超大单净额用于第二条累计线。

        键必须是主图用的同一种日期写法，否则这天会被当成「没数据」而留空——
        宁可空着也不猜日期。`jumbo` 缺省时只画主力一条累计线，不拿主力净额去顶替。
        """
        self._flow = {str(k): float(v) for k, v in (flow or {}).items()
                      if v is not None}
        self._jumbo = {str(k): float(v) for k, v in (jumbo or {}).items()
                       if v is not None}
        if source_label:
            self.source_label = source_label
        if data_level:
            self.data_level = data_level
        self.empty_reason = ("无数据：资金流尚未返回" if self._flow
                             else "无数据：该股在资金流表中没有记录")
        # 标题按附图只留「主力资金」，来源与等级放进悬浮提示，不再挤占画布
        if self.source_label or self.data_level:
            self.setToolTip(f"数据来源：{self.source_label or 'stockdb 资金流'}"
                            f"（{self.data_level or '-'} 级）")
        self.update()

    def clear_flow(self, reason: str = "无数据：资金流尚未返回") -> None:
        self._flow = {}
        self._jumbo = {}
        self._hover_key = ""
        self.empty_reason = reason
        self.update()

    def _on_hover(self, payload) -> None:
        key = (payload or {}).get("time", "") if isinstance(payload, dict) else ""
        if key != self._hover_key:
            self._hover_key = key
            self.update()

    # ------------------------------------------------------------ 绘制
    def _draw_legend(self, painter: QPainter, items: list[tuple[str, QColor]],
                     right: float, left_limit: float = 0.0) -> None:
        """右上角图例：线样 + 文字，从右往左排，避免和左侧标题抢位置。

        放不下的条目直接不画：宁可少一条图例，也不让它压到标题上变成一团字。
        """
        painter.setFont(QFont(UI_FAMILY, 8))
        metrics = painter.fontMetrics()
        x = right
        for label, color in reversed(items):
            width = metrics.horizontalAdvance(label)
            if x - width - 18 < left_limit:
                return
            painter.setPen(QPen(color))
            painter.drawText(QRectF(x - width, 1, width, 14),
                             Qt.AlignLeft | Qt.AlignVCenter, label)
            x -= width + 6
            painter.setPen(QPen(color, 1.6))
            painter.drawLine(QPointF(x - 13, 8.5), QPointF(x, 8.5))
            x -= 18

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)

        bars = self._candles.visible_bars() if self._candles else []
        if not self._flow:
            self._draw_empty(painter, self.empty_reason)
            return
        if not bars:
            self._draw_empty(painter, "无数据：主图尚未加载K线")
            return

        keys = [bar.bar_time.strftime("%Y-%m-%d") for bar in bars]
        main_values = [self._flow.get(key) for key in keys]
        if all(value is None for value in main_values):
            # 有资金流、但一个都不落在这段K线里：多半是主图区间早于资金流起始日
            self._draw_empty(painter, "无数据：当前K线区间没有对应的资金流记录")
            return

        jumbo_values = [self._jumbo.get(key) for key in keys]
        cum_main = _cumulative(main_values)
        cum_jumbo = _cumulative(jumbo_values)

        plot = QRectF(self.LEFT, self.TITLE_H,
                      max(10, self.width() - self.LEFT - self.RIGHT),
                      max(20, self.height() - self.TITLE_H - self.AXIS_H))

        # y 轴同时容纳当日柱与累计线；0 必须落在轴内，否则看不出哪根是流出
        samples = [value for value in (main_values + cum_main + cum_jumbo)
                   if value is not None]
        lo, hi = min(samples + [0.0]), max(samples + [0.0])
        if hi <= lo:
            hi = lo + 1.0
        # 上下各留 6%：柱顶/线顶不贴边。刻度只标「落在区间内的整数亿/万」，
        # 不再把坐标轴向外凑到整刻度——凑出去会在顶部留一大片空白（累计动辄
        # 上百亿时特别明显），数据本身反而被压扁。
        pad = (hi - lo) * 0.06
        lo -= pad
        hi += pad
        step = _nice_step(hi - lo, 5)

        def y_of(value: float) -> float:
            return plot.bottom() - (value - lo) / (hi - lo) * plot.height()

        # 网格 + 右侧金额刻度（亿/万）
        painter.setFont(self._axis_font())
        tick = math.ceil(lo / step) * step
        for _ in range(12):
            if tick > hi + step * 1e-9:
                break
            y = y_of(tick)
            painter.setPen(self._grid_pen())
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(DIM))
            painter.drawText(QRectF(plot.right() + 4, y - 8, self.RIGHT - 6, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, _fmt_yuan(tick))
            tick += step

        # 零轴再压一道实线：净额有正有负，这条线是「流入/流出」的分界
        zero = y_of(0.0)
        painter.setPen(QPen(GRID.lighter(170), 1.0))
        painter.drawLine(QPointF(plot.left(), zero), QPointF(plot.right(), zero))

        slot = plot.width() / len(bars)
        body_w = max(1.0, slot * 0.62)

        # 当日净额柱：红流入 / 绿流出
        for idx, value in enumerate(main_values):
            if value is None:
                continue               # 停牌/未同步的那天留白，不画 0 柱
            cx = plot.left() + slot * (idx + 0.5)
            y = y_of(value)
            top, bottom = (y, zero) if value >= 0 else (zero, y)
            painter.setPen(Qt.NoPen)
            painter.setBrush(UP if value >= 0 else DOWN)
            painter.drawRect(QRectF(cx - body_w / 2, top,
                                    body_w, max(1.0, bottom - top)))

        # 累计线：超大单（粉）在下、主力（黄）在上，主口径压在最顶避免被盖住
        for series, color in ((cum_jumbo, QColor(self.CUM_JUMBO_COLOR)),
                              (cum_main, QColor(self.CUM_MAIN_COLOR))):
            if all(value is None for value in series):
                continue
            painter.setPen(QPen(color, 1.3))
            painter.setBrush(Qt.NoBrush)
            poly = QPolygonF()
            for idx, value in enumerate(series):
                if value is None:
                    if len(poly) > 1:
                        painter.drawPolyline(poly)
                    poly = QPolygonF()
                    continue
                poly.append(QPointF(plot.left() + slot * (idx + 0.5), y_of(value)))
            if len(poly) > 1:
                painter.drawPolyline(poly)

        # 悬停：竖线定位到主图十字光标那一天
        if self._hover_key in keys:
            idx = keys.index(self._hover_key)
            cx = plot.left() + slot * (idx + 0.5)
            painter.setPen(QPen(QColor(COLORS["text_dim"]), 0.8, Qt.DashLine))
            painter.drawLine(QPointF(cx, plot.top()), QPointF(cx, plot.bottom()))

        # 标题行：左「主力资金」，右图例（按实际有数据的线给，不画空图例）
        painter.setFont(QFont(UI_FAMILY, 9))
        painter.setPen(QPen(DIM))
        painter.drawText(QRectF(plot.left(), 0, 120, self.TITLE_H),
                         Qt.AlignLeft | Qt.AlignVCenter, "主力资金")
        legend: list[tuple[str, QColor]] = []
        if any(value is not None for value in cum_main):
            legend.append(("累计主力净流入", QColor(self.CUM_MAIN_COLOR)))
        if any(value is not None for value in cum_jumbo):
            legend.append(("累计超大单净流入", QColor(self.CUM_JUMBO_COLOR)))
        if legend:
            self._draw_legend(painter, legend, plot.right(), plot.left() + 76)

        # 悬停读当日数值（悬停优先，否则最新一根）
        focus_idx = (keys.index(self._hover_key)
                     if self._hover_key in keys else len(bars) - 1)
        parts = [f"{keys[focus_idx]}"]
        main_value = main_values[focus_idx]
        if main_value is not None:
            parts.append(f"主力 {_fmt_signed(main_value)}")
        jumbo_value = jumbo_values[focus_idx]
        if jumbo_value is not None:
            parts.append(f"超大单 {_fmt_signed(jumbo_value)}")
        cum_value = cum_main[focus_idx]
        if cum_value is not None:
            parts.append(f"累计 {_fmt_signed(cum_value)}")
        if len(parts) > 1:
            text = "  ".join(parts)
            painter.setFont(QFont(MONO_FAMILY, 8))
            # 读数压在柱子和累计线上面：垫一层半透明底，否则撞上大柱就看不清
            width = painter.fontMetrics().horizontalAdvance(text) + 10
            chip = QRectF(plot.left() + 1, plot.bottom() - 15,
                          min(width, plot.width() - 2), 13)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(0, 0, 0, 190))
            painter.drawRoundedRect(chip, 2, 2)
            if main_value is None:
                color = DIM
            else:
                color = UP if main_value >= 0 else DOWN
            painter.setPen(QPen(color))
            painter.drawText(chip.adjusted(5, 0, 0, 0),
                             Qt.AlignLeft | Qt.AlignVCenter, text)

        # 日期轴：与主图同一条时间刻度，跨月也能看出区间跨度
        painter.setPen(QPen(DIM))
        painter.setFont(self._axis_font())
        for frac in (0, 0.25, 0.5, 0.75, 1.0):
            idx = min(len(bars) - 1, int(frac * (len(bars) - 1)))
            x = plot.left() + slot * (idx + 0.5)
            left = min(max(plot.left(), x - 40), plot.right() - 80)
            painter.drawText(QRectF(left, plot.bottom() + 1, 80, self.AXIS_H - 1),
                             Qt.AlignCenter, keys[idx])


class IntradayChart(_ChartBase):
    """采样分时：价格线 + 均价线 + 昨收/涨跌停参考线 + 方向着色量柱 + 区间大单标记。"""

    point_clicked = Signal(object)     # datetime

    TOTAL_SLOTS = TOTAL_SLOTS

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.points: list[SamplePoint] = []
        self.prev_close: float | None = None
        self.limit_up: float | None = None
        self.limit_down: float | None = None
        self.title = ""
        self.data_level = "B"
        self.markers: list = []          # list[LargeOrder]，区间口径
        self.focus_time: datetime | None = None
        self.source = "sampled"
        self.empty_reason = "无数据：分时尚未积累数据点"

    def set_series(self, points: list[SamplePoint], prev_close: float | None,
                   limit_up: float | None = None, limit_down: float | None = None,
                   title: str = "", data_level: str = "B",
                   source: str = "sampled") -> None:
        self.points = list(points)
        self.prev_close = prev_close
        self.limit_up = limit_up
        self.limit_down = limit_down
        self.title = title
        self.data_level = data_level
        self.source = source          # minute=真分钟线（东财/free-stockdb）/ sampled=快照差分
        # 两种来源的空状态不能共用一句话：分钟线是「取数还没回来」，采样是「点还没攒够」
        self.empty_reason = ("分钟线尚未返回：等待本轮拉取结果"
                             if is_minute_source(source) else "无数据：分时尚未积累数据点")
        self.update()

    def set_markers(self, orders: list) -> None:
        """区间大单标记：三角形落在成交价上，档位越高越大。"""
        self.markers = list(orders or [])
        self.update()

    def set_focus(self, moment: datetime | None) -> None:
        self.focus_time = moment
        self.update()

    @staticmethod
    def _slot_of(moment: datetime) -> int:
        return intraday_slot(moment)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.points:
            return
        plot = self._plot_rect()
        pos = event.position()
        if not plot.contains(pos):
            return
        slot = int((pos.x() - plot.left()) / plot.width() * (self.TOTAL_SLOTS - 1)) \
            if plot.width() else 0
        nearest = min(self.points, key=lambda p: abs(self._slot_of(p.time) - slot))
        self.focus_time = nearest.time
        self.update()
        self.point_clicked.emit(nearest.time)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._hover = event.position()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)

        if len(self.points) < 2:
            hint = self.empty_reason if not self.points else (
                f"数据不足：已收到 {len(self.points)} 个点，至少需要 2 个点才能连线")
            self._draw_empty(painter, hint)
            kind = "分钟分时" if is_minute_source(self.source) else "采样分时"
            self._draw_badge(painter, f"{kind} · 数据不足", WARN)
            return

        plot = self._plot_rect()
        vol = self._vol_rect()
        prices = [p.price for p in self.points]
        base = self.prev_close or prices[0]
        data_span = max(abs(max(prices) - base), abs(base - min(prices)))
        if data_span <= 0:
            data_span = base * 0.01
        span = max(data_span * 1.15, base * 0.004)
        if self.limit_up and abs(self.limit_up - base) <= span * 2:
            span = max(span, abs(self.limit_up - base) * 1.02)
        if self.limit_down and abs(base - self.limit_down) <= span * 2:
            span = max(span, abs(base - self.limit_down) * 1.02)
        hi, lo = base + span, base - span

        def y_of(price: float) -> float:
            return plot.bottom() - (price - lo) / (hi - lo) * plot.height()

        painter.setFont(self._axis_font())
        for i in range(5):
            price = lo + (hi - lo) * i / 4
            y = y_of(price)
            painter.setPen(self._grid_pen())
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(color_for_change(price - base)))
            painter.drawText(QRectF(plot.right() + 4, y - 8, 58, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, f"{price:.2f}")

        def x_of(slot: int) -> float:
            return plot.left() + plot.width() * slot / (self.TOTAL_SLOTS - 1)

        # 昨收 / 涨跌停参考线
        painter.setPen(QPen(DIM, 1, Qt.DashLine))
        painter.drawLine(QPointF(plot.left(), y_of(base)), QPointF(plot.right(), y_of(base)))
        if self.limit_up and lo <= self.limit_up <= hi:
            painter.setPen(QPen(UP, 1, Qt.DashLine))
            painter.drawLine(QPointF(plot.left(), y_of(self.limit_up)),
                             QPointF(plot.right(), y_of(self.limit_up)))
        if self.limit_down and lo <= self.limit_down <= hi:
            painter.setPen(QPen(DOWN, 1, Qt.DashLine))
            painter.drawLine(QPointF(plot.left(), y_of(self.limit_down)),
                             QPointF(plot.right(), y_of(self.limit_down)))

        # 中线（午休分隔）
        painter.setPen(QPen(GRID, 1))
        painter.drawLine(QPointF(x_of(120), plot.top()), QPointF(x_of(120), vol.bottom()))

        # 价格线与均价线；采样间隔 > 3 倍中位数处断开
        gaps = []
        intervals = [(b.time - a.time).total_seconds()
                     for a, b in zip(self.points, self.points[1:])]
        if intervals:
            ordered = sorted(intervals)
            median = ordered[len(ordered) // 2]
            threshold = max(10.0, median * 3)
            gaps = [i for i, gap in enumerate(intervals) if gap > threshold]

        def build(values: list[float | None]) -> list[QPolygonF]:
            polys: list[QPolygonF] = []
            current = QPolygonF()
            for idx, value in enumerate(values):
                if value is None:
                    if len(current) > 1:
                        polys.append(current)
                    current = QPolygonF()
                    continue
                if idx - 1 in gaps and len(current) > 1:
                    polys.append(current)
                    current = QPolygonF()
                current.append(QPointF(x_of(self._slot_of(self.points[idx].time)), y_of(value)))
            if len(current) > 1:
                polys.append(current)
            return polys

        price_polys = build([p.price for p in self.points])
        avg_polys = build([p.avg_price for p in self.points])

        # 价格线下方浅色填充
        if price_polys:
            path = QPainterPath()
            first = price_polys[0]
            path.moveTo(first[0])
            for point in list(first)[1:]:
                path.lineTo(point)
            path.lineTo(QPointF(first[len(first) - 1].x(), plot.bottom()))
            path.lineTo(QPointF(first[0].x(), plot.bottom()))
            path.closeSubpath()
            fill = QColor(UP if self.points[-1].price >= base else DOWN)
            fill.setAlpha(28)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawPath(path)

        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor(COLORS["text"]), 1.3))
        for poly in price_polys:
            painter.drawPolyline(poly)
        painter.setPen(QPen(QColor(MA_COLORS[0]), 1.1))
        for poly in avg_polys:
            painter.drawPolyline(poly)

        # 量柱
        max_amount = max((p.interval_amount for p in self.points), default=0) or 1
        painter.setPen(Qt.NoPen)
        bar_w = max(1.0, plot.width() / self.TOTAL_SLOTS * 0.8)
        for point in self.points:
            if point.interval_volume <= 0:
                continue
            h = point.interval_amount / max_amount * (vol.height() - 2)
            color = UP if point.direction == "buy" else DOWN if point.direction == "sell" else MUTED
            painter.setBrush(color)
            painter.drawRect(QRectF(x_of(self._slot_of(point.time)) - bar_w / 2,
                                    vol.bottom() - h, bar_w, max(0.8, h)))

        # 区间大单标记（口径：区间累计，不是单笔）
        for order in self.markers:
            slot = self._slot_of(order.time)
            x = x_of(slot)
            y = max(plot.top() + 4, y_of(order.price))
            color = (UP if order.direction == "buy"
                     else DOWN if order.direction == "sell" else MUTED)
            size = 5.0 if order.tier in MARKER_TIERS else 3.4
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawPolygon(QPolygonF([
                QPointF(x, y - 7),
                QPointF(x - size, y - 7 - size * 1.3),
                QPointF(x + size, y - 7 - size * 1.3),
            ]))
            painter.setPen(QPen(color, 0.8))
            painter.drawLine(QPointF(x, y - 7), QPointF(x, y))

        # 联动定位：竖直高亮线 + 对应量柱描边
        if self.focus_time is not None:
            slot = self._slot_of(self.focus_time)
            x = x_of(slot)
            painter.setPen(QPen(INFO, 1, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, vol.bottom()))
            nearest = min(self.points, key=lambda p: abs(self._slot_of(p.time) - slot))
            if nearest.interval_volume > 0:
                h = nearest.interval_amount / max_amount * (vol.height() - 2)
                painter.setPen(QPen(INFO, 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(QRectF(x - bar_w / 2 - 1, vol.bottom() - h - 1,
                                        bar_w + 2, h + 2))

        # 时间刻度
        painter.setPen(QPen(DIM))
        painter.setFont(self._axis_font())
        marks = [(0, "09:30"), (60, "10:30"), (120, "11:30/13:00"),
                 (180, "14:00"), (240, "15:00")]
        if plot.width() < 620:
            marks = [(0, "09:30"), (120, "11:30/13:00"), (240, "15:00")]
        for slot, label in marks:
            x = x_of(slot)
            left = min(max(plot.left(), x - 40), plot.right() - 80)
            painter.drawText(QRectF(left, vol.bottom() + 2, 80, 14), Qt.AlignCenter, label)

        # 十字光标
        if self._hover and plot.contains(self._hover):
            slot = int((self._hover.x() - plot.left()) / plot.width() * (self.TOTAL_SLOTS - 1))
            nearest = min(self.points, key=lambda p: abs(self._slot_of(p.time) - slot))
            x = x_of(self._slot_of(nearest.time))
            painter.setPen(QPen(QColor(COLORS["text_dim"]), 0.8, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, vol.bottom()))
            painter.drawLine(QPointF(plot.left(), self._hover.y()),
                             QPointF(plot.right(), self._hover.y()))
            chg = (nearest.price - base) / base * 100 if base else 0.0
            text = f"{nearest.time.strftime('%H:%M:%S')}  {nearest.price:.2f}  {chg:+.2f}%"
            painter.setPen(QPen(TEXT))
            painter.setFont(QFont(MONO_FAMILY, 8))
            painter.drawText(QRectF(plot.left() + 2, plot.bottom() - 15, 300, 14),
                             Qt.AlignLeft, text)

        kind = "分钟分时" if is_minute_source(self.source) else "采样分时"
        self._draw_badge(painter, f"{kind} · {self.data_level}级", WARN)
        painter.setPen(QPen(DIM))
        painter.setFont(QFont(MONO_FAMILY, 8))
        # 标题宽度跟着画布走：写死 260 会把「东方财富 1 分钟聚合，非逐笔」截掉半句
        painter.drawText(QRectF(plot.left() + 2, plot.top() + 2,
                                max(160.0, plot.width() - 6), 14),
                         Qt.AlignLeft, self.title)


class IntradayMoneyFlowChart(_ChartBase):
    """分时页下方「资金流」副图：五条**当日累计净额**曲线。

    为什么要单独一张图而不是叠在分时上：
    - 价格单位是「元/股」，资金是「元」，共用一根 y 轴必然把其中一条压平；
    - 分时图已经用掉整屏高度画价格 + 量柱，再叠五条线会糊成一团。

    口径（与适配器一致，图上不二次加工）：
    - 接口 `fflow/kline?klt=1` 回的就是**累计净额**（09:31 起累加），直接连线；
    - 右轴单位是元，按万/亿简写；零轴单独一道虚线，用来区分净流入 / 净流出；
    - 五条线各自的颜色与图例一一对应，图例上直接给「当前累计」数值，
      不用把鼠标移上去才知道收盘时主力是进是出。

    时间轴与上方分时图**共用同一个 241 槽坐标**（`intraday_slot`），
    左右边距也照抄（LEFT=8 / RIGHT=64），两张图的 09:30、11:30、15:00 才在同一条竖线上。
    """

    LEFT = 8
    RIGHT = 64          # 与 IntradayChart._plot_rect() 的右边距一致
    TITLE_H = 18
    AXIS_H = 16
    PREFERRED_HEIGHT = 168
    # 相邻两个点跨过多少个分钟槽就断线：午休在槽位上是连续的（120→121），
    # 只有真的缺分钟（停牌、接口漏点）才会跳，断线比拉一条假直线诚实
    GAP_SLOTS = 6

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # 固定高度：五条线 + 图例 + 时间轴，再矮就挤在一起；窗口变矮时压上方分时图
        self.setFixedHeight(self.PREFERRED_HEIGHT)
        self.points: list = []
        self.day = None
        self.source_label = ""
        self.data_level = "B"
        self.empty_reason = "无数据：资金流尚未返回"
        self._hover_slot: int | None = None

    # ------------------------------------------------------------ 数据
    def set_points(self, points, source_label: str = "", data_level: str = "") -> None:
        """写入分钟资金流点（需带 `.time` 与五个净额字段），按时间升序重排。"""
        self.points = sorted(list(points or []), key=lambda p: p.time)
        self._hover_slot = None
        if source_label:
            self.source_label = source_label
        if data_level:
            self.data_level = data_level
        if self.points:
            self.day = self.points[-1].time.date()
            self.empty_reason = "无数据：资金流序列为空"
            if self.source_label or self.data_level:
                self.setToolTip(
                    f"数据来源：{self.source_label or '东方财富资金流'}（{self.data_level or '-'} 级）\n"
                    "口径：当日累计净额（元），主力 = 超大单 + 大单；接口原值直画，不做累加")
        else:
            self.day = None
        self.update()

    def clear_points(self, reason: str = "无数据：资金流尚未返回") -> None:
        """加载中 / 取数失败 / 配置关闭都走这里：清掉旧票的数据，只留一句原因。"""
        self.points = []
        self.day = None
        self._hover_slot = None
        self.empty_reason = reason
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(self.LEFT, self.TITLE_H,
                      max(10, self.width() - self.LEFT - self.RIGHT),
                      max(20, self.height() - self.TITLE_H - self.AXIS_H))

    @staticmethod
    def _values(points: list, key: str) -> list[float | None]:
        return [getattr(point, key, None) for point in points]

    @staticmethod
    def _x_of(plot: QRectF, slot: int) -> float:
        return plot.left() + plot.width() * max(0, min(TOTAL_SLOTS - 1, slot)) \
            / (TOTAL_SLOTS - 1)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        plot = self._plot()
        if self.points and plot.width() > 0 and plot.contains(pos):
            slot = int(round((pos.x() - plot.left()) / plot.width() * (TOTAL_SLOTS - 1)))
            self._hover_slot = max(0, min(TOTAL_SLOTS - 1, slot))
        else:
            self._hover_slot = None
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover_slot = None
        super().leaveEvent(event)

    def _nearest(self):
        if not self.points or self._hover_slot is None:
            return None
        return min(self.points,
                   key=lambda p: abs(intraday_slot(p.time) - self._hover_slot))

    # ------------------------------------------------------------ 绘制辅助
    def _draw_state(self, painter: QPainter, text: str) -> None:
        """空/加载/失败提示：自动换行，错误原文再长也不会被裁成半句。"""
        painter.setPen(QPen(MUTED))
        painter.setFont(QFont(UI_FAMILY, 9))
        painter.drawText(self._plot().adjusted(14, 0, -14, 0),
                         Qt.AlignCenter | Qt.TextWordWrap, text)

    def _legend_items(self, values: dict, plain: bool) -> list[tuple[str, QColor]]:
        """图例条目：`plain=True` 只留名称，用于窄画布（名称比数值更不能省）。

        只在 `values` 里出现的系列才画图例——整条序列都是空值时图上没有这条线，
        还挂一个「主力:--」会让人以为数据在加载。
        """
        items = []
        for key, label, color in FLOW_SERIES:
            if key not in values:
                continue
            text = (FLOW_SHORT_LABEL.get(label, label) if plain
                    else f"{label}:{_fmt_compact_yuan(values.get(key))}")
            items.append((text, QColor(color)))
        return items

    def _fit_legend(self, painter: QPainter, values: dict, available: float):
        """能放下带数值的就放，放不下退化成只写名称，再放不下就少画几条。

        返回 [(文字, 颜色, 宽度)]。数值被省掉不影响读图：鼠标悬停有完整读数，
        而曲线颜色与名称必须始终可见——那是「这条线是谁」的唯一线索。
        """
        for plain in (False, True):
            items = self._legend_items(values, plain)
            widths = [painter.fontMetrics().horizontalAdvance(text) + 10 for text, _ in items]
            if sum(widths) + 12 * len(items) <= available:
                return [(text, color, width)
                        for (text, color), width in zip(items, widths)]
        # 最窄的情况：至少保住前几条（主力/超大/大单最能说明问题）
        kept, used = [], 0.0
        for text, color in self._legend_items(values, True):
            width = painter.fontMetrics().horizontalAdvance(text) + 10
            if used + width + 12 > available and kept:
                break
            kept.append((text, color, width))
            used += width + 12
        return kept

    # ------------------------------------------------------------ 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)

        plot = self._plot()
        # 窄画布先给图例腾地方：标题只留「资金流·累计」，等级只留「B级」。
        # 宁可少一句解释，也不能把某条线的图例挤掉——线还在画，图例没了就没人知道它是谁。
        narrow = self.width() < 560
        badge = (f"{self.data_level}级" if narrow
                 else f"1分钟 · 累计 · {self.data_level}级")
        painter.setFont(QFont(MONO_FAMILY, 8))
        badge_w = painter.fontMetrics().horizontalAdvance(badge) + 20

        # 标题行：左「资金流」，中间图例，右角数据等级
        if narrow:
            title = "资金流 · 累计"
        elif self.day is None:
            title = "资金流"
        elif self.day == datetime.now().date():
            title = "资金流 · 当日累计净额"
        else:
            title = f"资金流 · {self.day:%m-%d}（最近交易日）"
        painter.setFont(QFont(UI_FAMILY, 9))
        painter.setPen(QPen(DIM))
        painter.drawText(QRectF(plot.left(), 0, 240, self.TITLE_H),
                         Qt.AlignLeft | Qt.AlignVCenter, title)
        title_w = painter.fontMetrics().horizontalAdvance(title)

        if not self.points:
            self._draw_badge(painter, badge, MUTED)
            self._draw_state(painter, self.empty_reason)
            return
        if len(self.points) < 2:
            self._draw_badge(painter, badge, WARN)
            self._draw_state(
                painter,
                f"资金流数据不足：已收到 {len(self.points)} 个分钟点，至少需要 2 个点才能连线")
            return

        values_by_key = {key: self._values(self.points, key) for key, _, _ in FLOW_SERIES}
        latest = {key: series[-1] for key, series in values_by_key.items()}
        # 图例只给「真的有线」的系列：整段空的档位不画线，也不该占图例
        present = {key: value for key, value in latest.items()
                   if any(v is not None for v in values_by_key[key])}

        # y 轴：五条线 + 0 一起定界，保证零轴一定在轴内（否则看不出谁在流出）
        samples = [value for series in values_by_key.values()
                   for value in series if value is not None]
        lo, hi = min(samples + [0.0]), max(samples + [0.0])
        if hi <= lo:
            hi = lo + 1.0
        pad = (hi - lo) * 0.08
        lo -= pad
        hi += pad

        def y_of(value: float) -> float:
            return plot.bottom() - (value - lo) / (hi - lo) * plot.height()

        step = _nice_step(hi - lo, 4)
        painter.setFont(self._axis_font())
        tick = math.ceil(lo / step) * step
        for _ in range(10):
            if tick > hi + step * 1e-9:
                break
            y = y_of(tick)
            painter.setPen(self._grid_pen())
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(DIM))
            painter.drawText(QRectF(plot.right() + 4, y - 8, self.RIGHT - 6, 16),
                             Qt.AlignLeft | Qt.AlignVCenter, _fmt_compact_yuan(tick))
            tick += step

        # 零轴：资金流最该看的是「翻正 / 翻负」，这道线要比普通网格重
        zero = y_of(0.0)
        painter.setPen(QPen(GRID.lighter(190), 1.0, Qt.DashLine))
        painter.drawLine(QPointF(plot.left(), zero), QPointF(plot.right(), zero))

        # 五条曲线：小单先画，主力压在最上层，避免主口径被别的线盖住
        for key, _label, color in reversed(FLOW_SERIES):
            series = values_by_key[key]
            if all(value is None for value in series):
                continue
            painter.setPen(QPen(QColor(color), 1.3))
            painter.setBrush(Qt.NoBrush)
            for poly in self._polylines(series, plot, y_of):
                painter.drawPolyline(poly)

        # 时间刻度（与分时图同一批槽位；宽度不够就减少刻度，不重叠）
        marks = [(0, "09:30"), (120, "11:30/13:00"), (240, "15:00")]
        if plot.width() >= 620:
            marks = [(0, "09:30"), (60, "10:30"), (120, "11:30/13:00"),
                     (180, "14:00"), (240, "15:00")]
        if plot.width() >= 860:
            marks = [(0, "09:30"), (30, "10:00"), (60, "10:30"), (90, "11:00"),
                     (120, "11:30/13:00"), (150, "13:30"), (180, "14:00"),
                     (210, "14:30"), (240, "15:00")]
        painter.setPen(QPen(DIM))
        painter.setFont(self._axis_font())
        axis_y = plot.bottom() + 1
        for slot, label in marks:
            x = self._x_of(plot, slot)
            painter.setPen(QPen(GRID))
            painter.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 3))
            painter.setPen(QPen(DIM))
            left = min(max(plot.left(), x - 34), plot.right() - 68)
            painter.drawText(QRectF(left, axis_y, 68, self.AXIS_H - 1),
                             Qt.AlignCenter, label)

        # 悬浮：竖线 + 各线取点 + 读数浮层（比只标一条线更容易对账）
        nearest = self._nearest()
        if nearest is not None:
            slot = intraday_slot(nearest.time)
            x = self._x_of(plot, slot)
            painter.setPen(QPen(QColor(COLORS["text_dim"]), 0.8, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            for key, _label, color in FLOW_SERIES:
                value = getattr(nearest, key, None)
                if value is None:
                    continue
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(color))
                painter.drawEllipse(QPointF(x, y_of(value)), 2.4, 2.4)
            self._draw_hover_card(painter, plot, x, nearest)

        # 图例：标题之后、数据等级之前，带「当前累计」数值
        legend_start = plot.left() + title_w + 14
        available = (self.width() - badge_w - 10) - legend_start
        painter.setFont(QFont(UI_FAMILY, 8))
        items = self._fit_legend(painter, present, available)
        x = legend_start
        for text, color, width in items:
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(x + 3.5, self.TITLE_H / 2), 3.2, 3.2)
            painter.setPen(QPen(color))
            painter.drawText(QRectF(x + 10, 0, width, self.TITLE_H),
                             Qt.AlignLeft | Qt.AlignVCenter, text)
            x += width + 12

        self._draw_badge(painter, badge, MUTED)

    def _polylines(self, series: list, plot: QRectF, y_of) -> list[QPolygonF]:
        """按槽位连线；缺值或跨过 GAP_SLOTS 个槽才断开，不拉一条假直线。"""
        out: list[QPolygonF] = []
        current = QPolygonF()
        prev_slot: int | None = None
        for point, value in zip(self.points, series):
            if value is None:
                if len(current) > 1:
                    out.append(current)
                current, prev_slot = QPolygonF(), None
                continue
            slot = intraday_slot(point.time)
            if prev_slot is not None and slot - prev_slot > self.GAP_SLOTS:
                if len(current) > 1:
                    out.append(current)
                current = QPolygonF()
            current.append(QPointF(self._x_of(plot, slot), y_of(value)))
            prev_slot = slot
        if len(current) > 1:
            out.append(current)
        return out

    def _draw_hover_card(self, painter: QPainter, plot: QRectF, x: float, point) -> None:
        """鼠标处读数卡：时间 + 五档净额，颜色与曲线一致。"""
        rows = [(point.time.strftime("%H:%M"), DIM)]
        for key, label, color in FLOW_SERIES:
            value = getattr(point, key, None)
            text = "--" if value is None else _fmt_compact_signed(value)
            rows.append((f"{label} {text}", QColor(color)))

        painter.setFont(QFont(MONO_FAMILY, 8))
        metrics = painter.fontMetrics()
        width = max(metrics.horizontalAdvance(text) for text, _ in rows) + 16
        line_h = 13
        height = line_h * len(rows) + 8
        left = x + 12 if x + 12 + width <= plot.right() else x - width - 12
        left = max(plot.left() + 1, min(left, plot.right() - width - 1))
        top = plot.top() + 2

        box = QRectF(left, top, width, height)
        painter.setPen(QPen(GRID, 1.0))
        painter.setBrush(QColor(0, 0, 0, 215))
        painter.drawRoundedRect(box, 3, 3)
        for idx, (text, color) in enumerate(rows):
            painter.setPen(QPen(color))
            painter.drawText(QRectF(box.left() + 8, box.top() + 4 + idx * line_h,
                                    box.width() - 10, line_h),
                             Qt.AlignLeft | Qt.AlignVCenter, text)


class MiniChart(_ChartBase):
    """多股网格里的小图：价格曲线按分钟槽绘制，十字光标按时间槽跨图共享。"""

    crosshair = Signal(int)     # 0..240 分钟槽

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.points: list[SamplePoint] = []
        self.prev_close: float | None = None
        self.title = ""
        self.data_level = "B"
        self.source = "sampled"
        self._sync_slot: int | None = None
        self.setMinimumHeight(90)
        self.empty_reason = "无数据"

    def set_series(self, points: list[SamplePoint], prev_close: float | None,
                   title: str, data_level: str, source: str = "sampled") -> None:
        self.points = list(points)
        self.prev_close = prev_close
        self.title = title
        self.data_level = data_level
        self.source = source          # em=东财分钟线 / sampled=快照差分
        self.update()

    def set_crosshair_slot(self, slot: int | None) -> None:
        self._sync_slot = slot
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(6, 16, self.width() - 12, self.height() - 26)

    def _x_of(self, slot: int) -> float:
        plot = self._plot()
        return plot.left() + plot.width() * max(0, min(TOTAL_SLOTS - 1, slot)) / (TOTAL_SLOTS - 1)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        pos = event.position()
        plot = self._plot()
        if plot.width() > 0:
            slot = int((pos.x() - plot.left()) / plot.width() * (TOTAL_SLOTS - 1))
            self.crosshair.emit(max(0, min(TOTAL_SLOTS - 1, slot)))
        self._hover = pos
        self.update()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), PANEL)
        plot = self._plot()

        painter.setPen(QPen(DIM))
        painter.setFont(QFont(MONO_FAMILY, 8))
        painter.drawText(QRectF(6, 1, self.width() - 12, 14), Qt.AlignLeft, self.title)

        if not self.points:
            self._draw_empty(painter, ("等待分钟线…" if is_minute_source(self.source) else "采样中…")
                             if self.data_level else "无数据")
            return

        prices = [p.price for p in self.points]
        base = self.prev_close or prices[0]
        span = max(abs(max(prices) - base), abs(base - min(prices))) or 0.01
        hi, lo = base + span * 1.05, base - span * 1.05

        def y_of(price: float) -> float:
            return plot.bottom() - (price - lo) / (hi - lo) * plot.height()

        painter.setPen(QPen(GRID, 1, Qt.DashLine))
        painter.drawLine(QPointF(plot.left(), y_of(base)), QPointF(plot.right(), y_of(base)))

        color = UP if prices[-1] >= base else DOWN
        poly = QPolygonF([QPointF(self._x_of(intraday_slot(p.time)), y_of(p.price))
                          for p in self.points])
        painter.setPen(QPen(color, 1.2))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolyline(poly)

        if self._sync_slot is not None:
            x = self._x_of(self._sync_slot)
            painter.setPen(QPen(QColor(COLORS["text_dim"]), 0.8, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))

        last = prices[-1]
        chg = (last - base) / base * 100 if base else 0.0
        painter.setPen(QPen(color))
        painter.setFont(QFont(MONO_FAMILY, 8))
        painter.drawText(QRectF(plot.right() - 90, 1, 90, 14),
                         Qt.AlignRight, f"{last:.2f} {chg:+.2f}%")

