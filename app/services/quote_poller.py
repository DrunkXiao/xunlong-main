"""行情轮询线程：UI 只负责取结果渲染，全部 IO 在工作线程完成。

设计要点：
- 单线程顺序拉取，避免 tqcenter 并发调用互相踩连接；
- 结果投进队列，UI 用定时器 drain，避免跨线程直接操作控件；
- 轮询只覆盖「当前分组 + 当前单股」，股票池 5000+ 不参与轮询；
- 每轮先做采样差分，再把采样点喂给大单识别器，事件随结果一起回传；
- 采样点按分钟写入 kline_history(period='1m', quality_flag='sampled')，重启可恢复当天分时。
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.db.models import Bar, Quote
from app.db.repo import Repo
from app.services.intraday import IntradaySampler, SamplePoint
from app.services.large_order import LargeOrder, LargeOrderDetector

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PollResult:
    quotes: list[Quote] = field(default_factory=list)
    points: list[SamplePoint] = field(default_factory=list)
    orders: list[LargeOrder] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    at: datetime = field(default_factory=datetime.now)
    ok: bool = True


class QuotePoller(threading.Thread):
    def __init__(self, adapter, sampler: IntradaySampler, repo: Repo,
                 interval_sec: float = 3.0, persist: bool = True,
                 detector: LargeOrderDetector | None = None) -> None:
        super().__init__(name="quote-poller", daemon=True)
        self.adapter = adapter
        self.sampler = sampler
        self.repo = repo
        self.detector = detector
        # 分时改走东财分钟线后，同一区间会有「采样点」和「分钟线」两套数据。
        # 这个闸决定该股票的分时由谁负责：返回 False 时本线程只产内存点，不判定大单、
        # 也不把采样写进 1m 表（否则会覆盖同一分钟的东财分钟线）。
        self.sampler_owns = lambda symbol: True
        self.interval = max(0.5, float(interval_sec))
        self.persist = persist

        self._symbols: list[str] = []
        self._focus: str | None = None
        self._lock = threading.Lock()
        self._queue: queue.Queue[PollResult] = queue.Queue(maxsize=64)
        self._wake = threading.Event()
        self._stop = threading.Event()

        self.fail_streak = 0
        self.last_error: str | None = None
        self.last_latency_ms: int | None = None
        self._minute_bars: dict[tuple[str, str], Bar] = {}

    # ---------------------------------------------------------------- 控制
    def update(self, symbols: list[str], focus: str | None = None) -> None:
        with self._lock:
            changed = symbols != self._symbols
            self._symbols = list(symbols)
            self._focus = focus
        if changed:
            self._wake.set()

    def set_interval(self, interval_sec: float) -> None:
        self.interval = max(0.5, float(interval_sec))
        self._wake.set()

    def request_refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def drain(self) -> list[PollResult]:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    @property
    def subscribed(self) -> list[str]:
        with self._lock:
            return list(self._symbols)

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                symbols = list(self._symbols)
                focus = self._focus
            if not symbols:
                self._wake.wait(0.5)
                self._wake.clear()
                continue

            started = time.time()
            quotes: list[Quote] = []
            points: list[SamplePoint] = []
            orders: list[LargeOrder] = []
            errors: list[str] = []
            for symbol in symbols:
                if self._stop.is_set():
                    return
                try:
                    quote = self.adapter.get_quote(symbol)
                except Exception as exc:  # 适配器已转 DataSourceError，这里兜底防线程退出
                    errors.append(f"{symbol}: {exc}")
                    continue
                quotes.append(quote)
                try:
                    point, fired = self._feed(quote, focus)
                except Exception as exc:
                    logger.exception("处理行情失败，跳过本次采样：%s", symbol)
                    errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
                    continue
                if point is not None:
                    points.append(point)
                orders.extend(fired)

            elapsed_ms = int((time.time() - started) * 1000)
            self.last_latency_ms = elapsed_ms
            if quotes:
                self.fail_streak = 0
                self.last_error = None
                if self.persist:
                    try:
                        self.repo.upsert_quotes(quotes)
                        if orders:
                            self.repo.insert_large_orders(orders)
                    except Exception:
                        logger.exception("保存行情轮询结果失败")
            else:
                self.fail_streak += 1
                self.last_error = errors[0] if errors else "无行情返回"

            result = PollResult(quotes=quotes, points=points, orders=orders,
                                errors=errors, elapsed_ms=elapsed_ms,
                                at=datetime.now(), ok=bool(quotes))
            try:
                self._queue.put_nowait(result)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    self._queue.put_nowait(result)
                except queue.Empty:
                    pass

            wait = max(0.2, self.interval - (time.time() - started))
            if self._wake.wait(wait):
                self._wake.clear()

    # ---------------------------------------------------------------- 采样
    def _feed(self, quote: Quote, focus: str | None) -> tuple[SamplePoint | None, list[LargeOrder]]:
        point = self.sampler.feed(quote)
        if point is None:
            return None, []
        owns = self.sampler_owns(quote.symbol)
        if owns and focus and quote.symbol == focus and point.interval_volume > 0:
            self._persist_minute_bar(point)
        orders: list[LargeOrder] = []
        if owns and self.detector and point.interval_volume > 0:
            orders = self.detector.feed(point, quote.data_level)
        return point, orders

    def _persist_minute_bar(self, point: SamplePoint) -> None:
        """把采样点并入所属分钟的 bar，重复写同一 key 即 UPSERT。"""
        stamp = point.time.replace(second=0, microsecond=0)
        key = (point.symbol, stamp.isoformat(timespec="seconds"))
        bar = self._minute_bars.get(key)
        if bar is None:
            bar = Bar(
                symbol=point.symbol, period="1m", bar_time=stamp,
                open=point.price, high=point.price, low=point.price, close=point.price,
                volume=point.interval_volume, amount=point.interval_amount,
                data_level="B", source="tqcenter:sampled", quality_flag="sampled",
            )
            self._minute_bars[key] = bar
        else:
            bar.high = max(bar.high, point.price)
            bar.low = min(bar.low, point.price)
            bar.close = point.price
            bar.volume += point.interval_volume
            bar.amount += point.interval_amount
        try:
            self.repo.upsert_bars([bar])
        except Exception:
            logger.exception("保存采样分钟线失败：%s", point.symbol)
        if len(self._minute_bars) > 2000:
            for old_key in list(self._minute_bars)[:1000]:
                self._minute_bars.pop(old_key, None)
