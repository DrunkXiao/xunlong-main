"""后台任务队列：K线加载、名称补全等慢操作不阻塞 UI。

单工作线程串行执行，避免同时打多条 tqcenter 连接；UI 用定时器取结果。
"""
from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(slots=True)
class JobDone:
    tag: str
    result: Any = None
    error: str | None = None
    meta: dict | None = None


class BackgroundJobs:
    def __init__(self) -> None:
        self._jobs: queue.Queue = queue.Queue()
        self._done: queue.Queue[JobDone] = queue.Queue()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="bg-jobs", daemon=True)
        self._thread.start()

    def submit(self, tag: str, fn: Callable[..., Any], *args,
               meta: dict | None = None, **kwargs) -> None:
        self._jobs.put((tag, fn, args, kwargs, meta))

    def poll(self, limit: int = 8) -> list[JobDone]:
        out = []
        for _ in range(limit):
            try:
                out.append(self._done.get_nowait())
            except queue.Empty:
                break
        return out

    def stop(self) -> None:
        self._stop.set()
        self._jobs.put(None)

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._jobs.get()
            if item is None:
                return
            tag, fn, args, kwargs, meta = item
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:  # 任何异常都带回 UI，不静默
                self._done.put(JobDone(tag=tag, error=f"{type(exc).__name__}: {exc}", meta=meta))
                continue
            self._done.put(JobDone(tag=tag, result=result, meta=meta))
