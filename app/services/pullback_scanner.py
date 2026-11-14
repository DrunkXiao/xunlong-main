"""强势回调扫描线程：全市场日线读盘 + 评估，跑在独立线程里。

为什么不用 `BackgroundJobs`：那是一条串行队列，一次全市场扫描要 60 秒左右，
排在它后面的 K 线/盘口请求会被一起堵住，切股就卡住了。扫描单独一条线程，
既不影响盘中轮询，也能在扫描途中被「停止」打断。

事件通过队列交给界面（与行情/分时轮询同一套模式），界面用定时器 drain()。
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime

from app.adapters.tdx_daily import TdxDailyError, TdxDailySource
from app.services.pullback import PullbackParams, ScanResult, scan


@dataclass(slots=True)
class ScanProgress:
    done: int
    total: int
    phase: str = ""


@dataclass(slots=True)
class ScanDone:
    result: ScanResult | None = None
    error: str | None = None
    at: datetime = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.at is None:
            self.at = datetime.now()


class PullbackScanner(threading.Thread):
    """按需扫描：request_scan() 排队一次，跑完发 ScanDone。"""

    def __init__(self, config: dict, limit: int = 0) -> None:
        super().__init__(name="pullback-scanner", daemon=True)
        self.config = config or {}
        self.limit = int(limit or ((self.config.get("pullback") or {}).get("limit") or 0))
        self._events: queue.Queue = queue.Queue(maxsize=64)
        self._pending = threading.Event()
        self._cancel = threading.Event()       # 中断当前这一轮
        self._exit = threading.Event()         # 退出线程
        self._last_progress = 0.0
        self.running = False
        self.last_error: str | None = None

    # ---------------------------------------------------------------- 控制
    def request_scan(self, limit: int | None = None) -> None:
        if limit is not None:
            self.limit = int(limit)
        self._cancel.clear()
        self._pending.set()

    def request_stop(self) -> None:
        """打断当前这一轮，已经算完的部分不落盘（结果不完整，宁可不给）。"""
        if self.running:
            self._cancel.set()

    def stop(self) -> None:
        self._exit.set()
        self._pending.set()
        self._cancel.set()

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        while not self._exit.is_set():
            if not self._pending.wait(0.3):
                continue
            self._pending.clear()
            if self._exit.is_set():
                return
            self._run_once()
            self._last_progress = 0.0

    def _run_once(self) -> None:
        self.running = True
        try:
            params = PullbackParams.from_config(self.config)
            source = TdxDailySource.from_config(self.config)
            result = scan(
                source, params, limit=self.limit,
                on_progress=self._on_progress, should_stop=self._cancel.is_set,
            )
            self.last_error = None
            self._emit(ScanDone(result=result))
        except TdxDailyError as exc:
            self.last_error = str(exc)
            self._emit(ScanDone(error=str(exc)))
        except Exception as exc:                      # 线程不能因为一次异常退出
            self.last_error = f"{type(exc).__name__}: {exc}"
            self._emit(ScanDone(error=self.last_error))
        finally:
            self.running = False
            self._cancel.clear()

    # ---------------------------------------------------------------- 事件
    def _on_progress(self, done: int, total: int, phase: str) -> None:
        now = time.time()
        if phase == "扫描日线" and done < total and now - self._last_progress < 0.25:
            return                                    # 节流，别让 UI 每 100 只都重排一次
        self._last_progress = now
        self._emit(ScanProgress(done=int(done), total=int(total), phase=phase))

    def _emit(self, event) -> None:
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()             # 丢最旧的一条，进度不能把结果挤掉
                self._events.put_nowait(event)
            except queue.Empty:
                pass
