"""分时数据引擎：两条来源，口径不同，必须分清楚。

**来源一：东方财富分钟线（首选，见 MinuteFeed）**
  一天 241 个点（09:30–11:30 121 个 + 13:00–15:00 120 个），每根分钟线带
  开/收/高/低/量/额/均价，量已换为股。这是分钟聚合，不是逐笔；
  方向只能由「本分钟收价 vs 上一分钟收价」推演，quality_flag = `em_minute,price_proxy`。
  零成交分钟是真实存在的（该分钟无撮合），标 `no_trade`，与「没取到」区分。

**来源二：tqcenter 快照差分采样（回落，见 IntradaySampler）**
  区间成交量 = 本次快照累计成交量 - 上次快照累计成交量
  区间成交额 = 本次累计成交额 - 上次
  区间均价   = 本次累计成交额 / 累计成交量
  方向       = 外盘增量 vs 内盘增量（优先）；无内外盘时用价格变动方向，标 price_proxy
  无新增数据 = 区间量为 0，标 no_update，图上是断点不是零成交

两者都产出 SamplePoint，图表/大单识别/预警无需改口径；界面上必须标清当前用的是哪一种。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from app.adapters.minute import EM_FLAG, MINUTE_FLAGS, STOCKDB_FLAG
from app.db.models import Quote

MAX_POINTS = 6000
# 分钟线来源标记集中在 adapters/minute.py：东财与 free-stockdb 各有标记，
# 落库后必须能分清是哪一路给的，不能互相冒充。
MINUTE_SOURCES = {
    EM_FLAG: ("eastmoney:trends", "B", "东方财富 1 分钟"),
    STOCKDB_FLAG: ("stockdb:minute", "C", "free-stockdb 本地库 1 分钟"),
}


def minute_source_meta(flag: str) -> tuple[str, str, str]:
    """标记 → (source, data_level, 界面文案)。认不出就按东财口径标，不让字段为空。"""
    for key, meta in MINUTE_SOURCES.items():
        if key in (flag or ""):
            return meta
    return MINUTE_SOURCES[EM_FLAG]


def is_minute_flag(flag: str) -> bool:
    """这条记录是不是真分钟线（而不是快照采样）。两个来源都算。"""
    return any(key in (flag or "") for key in MINUTE_FLAGS)


@dataclass(slots=True)
class SamplePoint:
    symbol: str
    time: datetime
    price: float
    avg_price: float | None
    interval_volume: int          # 股
    interval_amount: float        # 元
    cumulative_volume: int
    cumulative_amount: float
    direction: str                # buy / sell / unknown
    quality_flag: str             # sampled / no_update / price_proxy
    interval_span_sec: float = 0.0  # 与上一个采样点的实际间隔（秒）


@dataclass(slots=True)
class _Baseline:
    day: int
    volume: int
    amount: float
    inside: int | None
    outside: int | None
    price: float


class IntradaySampler:
    def __init__(self) -> None:
        self._baseline: dict[str, _Baseline] = {}
        self._series: dict[str, list[SamplePoint]] = {}
        self._prev_time: dict[str, datetime] = {}

    # ---------------------------------------------------------------- 采样
    def feed(self, quote: Quote, now: datetime | None = None) -> SamplePoint | None:
        """返回 None 表示首帧或跨日重置（无差分基线，不产生数据点）。"""
        if quote.price is None or quote.volume is None:
            return None
        now = now or quote.received_at or datetime.now()
        day_key = now.toordinal()
        prev = self._baseline.get(quote.symbol)

        if prev is None or prev.day != day_key or quote.volume < prev.volume:
            self._baseline[quote.symbol] = _Baseline(
                day=day_key, volume=quote.volume, amount=float(quote.amount or 0.0),
                inside=quote.inside_volume, outside=quote.outside_volume, price=quote.price,
            )
            self._series[quote.symbol] = []
            self._prev_time[quote.symbol] = now
            return None

        if prev.volume == 0:
            # 上一个基线是「空快照」（volume/amount 都是 0，常见于刚连上或刚开盘）。
            # 这时做差会把当天累计量当成一个区间的成交量，凭空造出几十亿的假大单。
            # 只更新基线，不产点。
            self._baseline[quote.symbol] = _Baseline(
                day=day_key, volume=quote.volume, amount=float(quote.amount or 0.0),
                inside=quote.inside_volume, outside=quote.outside_volume, price=quote.price,
            )
            self._prev_time[quote.symbol] = now
            return None

        prev_time = self._prev_time.get(quote.symbol)
        span = max(0.0, (now - prev_time).total_seconds()) if prev_time else 0.0

        d_volume = quote.volume - prev.volume
        d_amount = float(quote.amount or 0.0) - prev.amount
        if d_amount < 0:
            d_amount = 0.0

        direction, quality = "unknown", "sampled"
        if quote.inside_volume is not None and quote.outside_volume is not None \
                and prev.inside is not None and prev.outside is not None:
            d_out = max(0, quote.outside_volume - prev.outside)
            d_in = max(0, quote.inside_volume - prev.inside)
            if d_out > d_in * 1.2:
                direction = "buy"
            elif d_in > d_out * 1.2:
                direction = "sell"
        else:
            delta = quote.price - prev.price
            if abs(delta) >= 0.005:
                direction = "buy" if delta > 0 else "sell"
            quality = "price_proxy"

        if d_volume <= 0:
            quality = "no_update" if quality == "sampled" else quality + ",no_update"

        cumulative_volume = quote.volume
        cumulative_amount = float(quote.amount or 0.0)
        avg_price = (cumulative_amount / cumulative_volume) if cumulative_volume else None

        point = SamplePoint(
            symbol=quote.symbol, time=now, price=quote.price, avg_price=avg_price,
            interval_volume=max(0, d_volume), interval_amount=max(0.0, d_amount),
            cumulative_volume=cumulative_volume, cumulative_amount=cumulative_amount,
            direction=direction, quality_flag=quality, interval_span_sec=span,
        )

        series = self._series.setdefault(quote.symbol, [])
        series.append(point)
        if len(series) > MAX_POINTS:
            del series[: len(series) - MAX_POINTS]

        self._baseline[quote.symbol] = _Baseline(
            day=day_key, volume=quote.volume, amount=cumulative_amount,
            inside=quote.inside_volume, outside=quote.outside_volume, price=quote.price,
        )
        self._prev_time[quote.symbol] = now
        return point

    # ---------------------------------------------------------------- 读取
    def series(self, symbol: str) -> list[SamplePoint]:
        return self._series.get(symbol, [])

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._baseline.clear()
            self._series.clear()
            self._prev_time.clear()
        else:
            self._baseline.pop(symbol, None)
            self._series.pop(symbol, None)
            self._prev_time.pop(symbol, None)

    def has_baseline(self, symbol: str) -> bool:
        return symbol in self._baseline


def rolling_amount_mean(points: list[SamplePoint], window: int = 20,
                        exclude_last: bool = True) -> float | None:
    """近 N 个采样区间的成交额均值，用于「成交额突增」判定。

    exclude_last=True 时不含当前区间（否则自己会把均值抬高）；样本不足返回 None。
    """
    source = points[:-1] if exclude_last else points
    values = [p.interval_amount for p in source[-window:]]
    if len(values) < min(5, window):
        return None
    return sum(values) / len(values)


def change_over(points: list[SamplePoint], minutes: float) -> tuple[float, float] | None:
    """取 now 与 minutes 分钟前的价格，返回 (涨跌幅%, 实际跨度秒)。样本不足返回 None。"""
    if len(points) < 2:
        return None
    last = points[-1]
    cutoff = last.time.timestamp() - minutes * 60
    base = None
    for point in reversed(points[:-1]):
        base = point
        if point.time.timestamp() <= cutoff:
            break
    if base is None or not base.price or base.time == last.time:
        return None
    span = (last.time - base.time).total_seconds()
    if span < 1:
        return None
    return (last.price - base.price) / base.price * 100, span


def points_from_sampled_bars(bars: list) -> list[SamplePoint]:
    """把落库的 1m bar 还原成采样点序列（重启后恢复当天分时用）。

    只还原真实落库的数据；没有数据的时段不补点、不插值。
    `quality_flag` 从库里读：em_minute 的分钟线与 sampled 的采样点混存时不会互相冒充。
    """
    points: list[SamplePoint] = []
    cum_volume = 0
    cum_amount = 0.0
    prev_time = None
    ref_price = None
    for bar in bars:
        volume = int(bar.volume or 0)
        amount = float(bar.amount or 0.0)
        cum_volume += volume
        cum_amount += amount
        span = (bar.bar_time - prev_time).total_seconds() if prev_time else 0.0
        prev_time = bar.bar_time
        flag = getattr(bar, "quality_flag", "") or "sampled"

        # 东财分钟线的方向本来就是「收价 vs 上一分钟收价」推出来的，库里没存方向，
        # 但按同样规则可以一比一还原；快照采样点的方向来自当时的内外盘增量，存不回来，
        # 只能保持 unknown——宁可标不明，也不能编一个方向出来。
        direction = "unknown"
        if EM_FLAG in flag:
            if volume <= 0:
                flag = f"{EM_FLAG},no_trade"
            else:
                delta = bar.close - ref_price if ref_price else 0.0
                direction = ("buy" if delta > 0.004 else
                             "sell" if delta < -0.004 else "unknown")
                flag = f"{EM_FLAG},price_proxy"
        elif span <= 0 and volume == 0:
            flag = f"{flag},no_update"

        points.append(SamplePoint(
            symbol=bar.symbol, time=bar.bar_time, price=bar.close,
            avg_price=(cum_amount / cum_volume) if cum_volume else None,
            interval_volume=volume, interval_amount=amount,
            cumulative_volume=cum_volume, cumulative_amount=cum_amount,
            direction=direction, quality_flag=flag, interval_span_sec=span,
        ))
        ref_price = bar.close
    return points


# ==================================================================== 分钟线来源
def points_from_minute_bars(bars: list, symbol: str = "",
                            prev_close: float | None = None,
                            flag: str = EM_FLAG) -> list[SamplePoint]:
    """分钟线 → SamplePoint 序列。东财与 free-stockdb 共用，形状一致。

    方向用「本分钟收价 vs 上一分钟收价」推演（首根对昨收），所以一律带 `price_proxy`；
    这不等于内外盘——分钟线里没有内外盘信息，不能假装有。
    区间量额就是该分钟的成交，无需差分；`interval_span_sec` 固定 60（缺分钟时为实际跨度）。

    `flag` 是来源标记（`em_minute` / `stockdb_minute`），会把来源写进每个点，
    换源后图例、落库、大单来源都能对上号。
    """
    points: list[SamplePoint] = []
    cum_volume = 0
    cum_amount = 0.0
    prev_time = None
    ref_price = prev_close
    for bar in bars:
        volume = int(getattr(bar, "volume", 0) or 0)
        amount = float(getattr(bar, "amount", 0.0) or 0.0)
        price = float(getattr(bar, "close", 0.0) or 0.0)
        moment = bar.time
        if prev_time is None:
            span = 60.0
        else:
            gap = (moment - prev_time).total_seconds()
            # 午休与跨日不该算成 60 秒；超出 90 秒就按真实跨度记，预警折算时才不会被拉偏
            span = 60.0 if 0 < gap <= 90 else max(0.0, gap)
        cum_volume += volume
        cum_amount += amount

        if volume <= 0:
            direction, point_flag = "unknown", f"{flag},no_trade"
        else:
            delta = price - ref_price if ref_price else 0.0
            if delta > 0.004:
                direction = "buy"
            elif delta < -0.004:
                direction = "sell"
            else:
                direction = "unknown"
            point_flag = f"{flag},price_proxy"

        points.append(SamplePoint(
            symbol=symbol or getattr(bar, "symbol", "") or "",
            time=moment, price=price,
            avg_price=getattr(bar, "avg_price", None),
            interval_volume=volume, interval_amount=amount,
            cumulative_volume=cum_volume, cumulative_amount=cum_amount,
            direction=direction, quality_flag=point_flag, interval_span_sec=span,
        ))
        ref_price = price
        prev_time = moment
    return points


class MinuteFeed:
    """分钟线的内存序列：整日覆盖式合并 + 只把「已收分钟」交出去判定。

    为什么要有 watermark：接口每次返回的是**整日**序列，若每次都把全部点喂给大单识别器，
    09:30 的一根巨量分钟会在 15:00 被反复判定、每次刷新都触发一遍告警。
    所以：
      - 首次载入只补图，`fed` 水位直接设在最后一根，不为历史分钟补发告警；
      - 之后只交出 (水位, 最后一根) 之间的分钟，即「已经有下一根、不会再有成交进来」的分钟；
      - 当前这一根仍在累积，等下一根出现后才判定。

    来源标记取自 `Trends.minute_flag`（东财 `em_minute` / free-stockdb
    `stockdb_minute`），构造参数只作为缺省值。这样即使主源回落备源，
    同一天里先后进来的两批点也不会被标成同一个来源。
    """

    def __init__(self, flag: str = EM_FLAG, source_label: str | None = None) -> None:
        self.flag = flag
        self.source_label = source_label or minute_source_meta(flag)[2]
        self._series: dict[str, list[SamplePoint]] = {}
        self._meta: dict[str, dict] = {}
        self._fed: dict[str, datetime] = {}
        self.last_ok: datetime | None = None
        self.last_error: str | None = None
        self.last_symbol: str = ""

    # ---------------------------------------------------------------- 写入
    def update(self, trends) -> tuple[list[SamplePoint], list[SamplePoint], list[SamplePoint]]:
        """写入一只股票的整日分钟线。

        返回 (整日序列, 本次可判定的已收分钟, 首载回放的已收分钟)。
        第三项只在第一次载入时非空：历史分钟要照常算大单记录（否则打开软件时
        「区间大单」整片空白），但不能补发告警——早上的巨量不该在下午弹窗。
        """
        symbol = trends.symbol
        flag = getattr(trends, "minute_flag", None) or self.flag
        points = points_from_minute_bars(trends.bars, symbol, trends.pre_close, flag=flag)
        old = self._series.get(symbol) or []
        if old and not points:
            # 空回包同样不能清空已有序列：一次失败不该让图从 241 点变 0 点
            self.last_error = f"{symbol} 分时回包为空，保留已有 {len(old)} 个分钟点"
            return old, [], []
        if old and points and old[-1].time.date() == points[-1].time.date() \
                and len(points) < len(old):
            # 东财是整日覆盖式返回，同一天的分钟条数只增不减。突然变少只能是残缺回包
            # （限流、断流重连、尾包截断），不能拿它覆盖手里完整的序列——否则分时图
            # 会瞬间塌成「已收到 1 个点」的空白页，直到下一轮成功才恢复。
            self.last_error = (f"{symbol} 分时回包不完整（{len(points)} < {len(old)} 根），"
                               f"保留已有序列")
            return old, [], []
        first_load = symbol not in self._series
        self._series[symbol] = points
        self._meta[symbol] = {
            "name": getattr(trends, "name", "") or "",
            "pre_close": trends.pre_close,
            "day": getattr(trends, "day", None),
            "source": getattr(trends, "source", ""),
            "data_level": getattr(trends, "data_level", "") or "",
            "minute_flag": flag,
            "fetched_at": getattr(trends, "fetched_at", datetime.now()),
            "count": len(points),
        }
        self.last_ok = datetime.now()
        self.last_error = None
        self.last_symbol = symbol
        if not points:
            return [], [], []

        if first_load:
            # 首载：水位放在最后一根，历史分钟只做记录回放，不补告警
            self._fed[symbol] = points[-1].time
            return points, [], (points[:-1] if len(points) > 1 else [])

        fed_upto = self._fed.get(symbol)
        if fed_upto is None:
            self._fed[symbol] = points[-1].time
            return points, [], []
        closed = points[:-1] if len(points) > 1 else []
        fresh = [p for p in closed if p.time > fed_upto]
        if fresh:
            self._fed[symbol] = fresh[-1].time
        return points, fresh, []

    def note_error(self, message: str) -> None:
        self.last_error = message

    # ---------------------------------------------------------------- 读取
    def has_data(self, symbol: str) -> bool:
        return bool(self._series.get(symbol))

    def series(self, symbol: str) -> list[SamplePoint]:
        return self._series.get(symbol) or []

    def pre_close(self, symbol: str) -> float | None:
        return (self._meta.get(symbol) or {}).get("pre_close")

    def meta(self, symbol: str) -> dict:
        return dict(self._meta.get(symbol) or {})

    def fed_upto(self, symbol: str) -> datetime | None:
        return self._fed.get(symbol)

    def reset(self, symbol: str | None = None) -> None:
        if symbol is None:
            self._series.clear()
            self._meta.clear()
            self._fed.clear()
        else:
            self._series.pop(symbol, None)
            self._meta.pop(symbol, None)
            self._fed.pop(symbol, None)


def minute_bars_to_rows(bars: list, symbol: str, source: str | None = None,
                        data_level: str | None = None,
                        flag: str = EM_FLAG) -> list:
    """分钟线 → 待落库的 Bar（保留开高低收，别退化成只有收盘价）。

    `source/data_level/flag` 缺省按东财口径，free-stockdb 调用时传自己的，
    这样 1m 表里两路数据能靠 `quality_flag` 区分，`load_bars(quality_contains=...)`
    才不会张冠李戴。
    """
    from app.db.models import Bar

    default_source, default_level, _ = minute_source_meta(flag)
    rows = []
    for bar in bars:
        rows.append(Bar(
            symbol=symbol, period="1m", bar_time=bar.time,
            open=bar.open, high=bar.high, low=bar.low, close=bar.close,
            volume=int(bar.volume or 0), amount=float(bar.amount or 0.0),
            data_level=data_level or default_level,
            source=source or default_source,
            quality_flag=flag,
        ))
    return rows


def minute_slot_of(moment: datetime) -> int:
    """分钟在 0..240 时间轴上的位置（与 charts.intraday_slot 完全同口径）。

    午休压平：11:30 与 13:00 都落在 120，13:01 是 121，15:00 是 240。
    这个函数给服务层算格位用；两边若不一致，多股共享十字光标就会错位。
    """
    minutes = (moment.hour - 9) * 60 + moment.minute - 30
    if moment.time() >= time(13, 0):
        minutes -= 90
    elif moment.time() > time(11, 30):
        minutes = 120
    return max(0, min(240, minutes))

