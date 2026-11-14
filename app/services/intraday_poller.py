"""分时轮询线程：向东财要分钟线，喂给 MinuteFeed，产出的已收分钟交给大单识别器。

为什么要单独一条线程：
- 东财是 HTTP，tqcenter 是本地插件；混在一个线程里，一次网络抖动会把行情轮询一起拖慢，
  而行情延迟阈值（latency_abort_ms）是用来判断行情新鲜度的，不该被分时请求污染。
- 分时是整日覆盖式返回（241 行），15 秒拉一次完全够用，不需要跟着 3 秒行情走。

节流：
- 焦点股每个 `refresh_sec` 拉一次（默认 15s）；
- 网格里其它股票每 `grid_cycles` 轮拉一次（默认 4 轮 ≈ 60s），避免 9 宫格把接口打满；
- 非交易时段降速到 `refresh_sec × 4`，收盘后再拉两次就不会有变化了。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.adapters.base import DataSourceError
from app.db.repo import Repo
from app.services.intraday import EM_FLAG, MinuteFeed, SamplePoint, minute_bars_to_rows
from app.services.large_order import LargeOrder, LargeOrderDetector


@dataclass(slots=True)
class IntradayUpdate:
    symbol: str
    series: list[SamplePoint] = field(default_factory=list)
    fresh: list[SamplePoint] = field(default_factory=list)     # 已收分钟，可判定
    orders: list[LargeOrder] = field(default_factory=list)            # 新产生的，可告警
    backfill_orders: list[LargeOrder] = field(default_factory=list)   # 首载回放，只记录不告警
    name: str = ""
    pre_close: float | None = None
    bars_written: int = 0
    ok: bool = True
    error: str | None = None
    at: datetime = field(default_factory=datetime.now)


class IntradayPoller(threading.Thread):
    def __init__(self, adapter, feed: MinuteFeed, repo: Repo,
                 refresh_sec: float = 15.0, grid_cycles: int = 4,
                 detector: LargeOrderDetector | None = None,
                 persist: bool = True, is_trading=None) -> None:
        super().__init__(name="intraday-poller", daemon=True)
        self.adapter = adapter
        self.feed = feed
        self.repo = repo
        self.detector = detector
        self.refresh = max(3.0, float(refresh_sec))
        self.grid_cycles = max(1, int(grid_cycles))
        self.persist = persist
        self.is_trading = is_trading or (lambda: True)

        self._symbols: list[str] = []
        self._focus: str | None = None
        self._lock = threading.Lock()
        self._queue: queue.Queue[IntradayUpdate] = queue.Queue(maxsize=32)
        self._wake = threading.Event()
        self._stop = threading.Event()

        self.fail_streak = 0
        self.last_error: str | None = None
        self.last_elapsed_ms: int | None = None
        self._cycle = 0

    # ---------------------------------------------------------------- 控制
    def update(self, symbols: list[str], focus: str | None = None) -> None:
        with self._lock:
            changed = symbols != self._symbols or focus != self._focus
            self._symbols = list(symbols)
            self._focus = focus
        if changed:
            self._wake.set()

    def request_refresh(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def drain(self) -> list[IntradayUpdate]:
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
                self._wait(0.5)
                continue

            targets: list[str] = []
            if focus:
                targets.append(focus)
            if self._cycle % self.grid_cycles == 0:
                targets.extend(s for s in symbols if s != focus)
            if not targets:
                targets = symbols[:1]
            self._cycle += 1

            started = time.time()
            for symbol in targets:
                if self._stop.is_set():
                    return
                self._fetch_one(symbol, focus)
            self.last_elapsed_ms = int((time.time() - started) * 1000)

            base = self.refresh if self.is_trading() else self.refresh * 4
            if self.fail_streak:
                base = min(120.0, base * (2 ** min(self.fail_streak, 3)))
            self._wait(max(1.0, base - (time.time() - started)))

    def _wait(self, seconds: float) -> None:
        if self._wake.wait(seconds):
            self._wake.clear()

    # ---------------------------------------------------------------- 单只
    def _fetch_one(self, symbol: str, focus: str | None) -> None:
        try:
            trends = self.adapter.trends(symbol)
        except DataSourceError as exc:
            self.fail_streak += 1
            self.last_error = exc.message
            self.feed.note_error(exc.message)
            self._put(IntradayUpdate(symbol=symbol, ok=False, error=exc.message))
            return
        except Exception as exc:                      # 兜底：线程不能因为一只票退出
            self.fail_streak += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._put(IntradayUpdate(symbol=symbol, ok=False, error=self.last_error))
            return

        self.fail_streak = 0
        self.last_error = None
        series, fresh, replay = self.feed.update(trends)

        orders: list[LargeOrder] = []
        if self.detector and fresh:
            for point in fresh:
                orders.extend(self.detector.feed(point, trends.data_level))
        backfill: list[LargeOrder] = []
        if self.detector and replay:
            for point in replay:
                backfill.extend(self.detector.feed(point, trends.data_level))

        written = 0
        if self.persist:
            try:
                # 来源跟着 Trends 走：主源回落备源时，同一天的两批点各标各的来源
                written = self.repo.upsert_bars(minute_bars_to_rows(
                    trends.bars, symbol,
                    source=getattr(trends, "source", None) or None,
                    data_level=getattr(trends, "data_level", None) or None,
                    flag=getattr(trends, "minute_flag", None) or EM_FLAG,
                ))
            except Exception:
                written = 0
            if orders or backfill:
                try:
                    self.repo.insert_large_orders(list(orders) + list(backfill))
                except Exception:
                    pass

        self._put(IntradayUpdate(
            symbol=symbol, series=series, fresh=fresh, orders=orders,
            backfill_orders=backfill, name=trends.name, pre_close=trends.pre_close,
            bars_written=written,
        ))

    def _put(self, update: IntradayUpdate) -> None:
        try:
            self._queue.put_nowait(update)
        except queue.Full:
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(update)
            except queue.Empty:
                pass
