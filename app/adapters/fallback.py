"""分时来源回落包装器：主源失败自动切备源，冷却期内不反复撞墙。

为什么要这一层而不是在适配器里写 if：
主源（free-stockdb）和备源（东方财富）是完全独立的取数协议，各自都会抛
`DataSourceError`。上层 `IntradayPoller` 只认「一次 trends() 调用」，
它不该关心到底是谁给的。把「先试谁、失败后多久再试」放在这一层，
两个适配器都能保持单一职责，配置里也能单独关掉回落。

冷却的必要性：本地引擎没启动时，每 15 秒一次 socket 连接会拖慢分时轮询，
也让日志刷满同一句话。失败后按 `cooldown_sec` 直接走备源，过期再探一次主源。
"""
from __future__ import annotations

import time

from app.adapters.base import DataSourceError

# 这些错误说明「主源此刻给不了可用数据」，值得立刻回落
FALLBACK_KINDS = {"network", "timeout", "stale", "not_found", "bad_payload", "bad_request"}


class FallbackIntradayAdapter:
    """先主源，失败（或冷却期内）走备源；两路都失败才向上抛。"""

    def __init__(self, primary, secondary, cooldown_sec: float = 120.0,
                 clock=time.monotonic) -> None:
        self.primary = primary
        self.secondary = secondary
        self.cooldown = max(0.0, float(cooldown_sec))
        self._clock = clock
        self._blocked_until = 0.0
        self.last_reason: str = ""
        self.last_source: str = ""
        self.fallbacks: int = 0

    # 供界面/日志显示用：实际来源以 Trends.minute_flag 为准
    @property
    def minute_flag(self) -> str:
        return getattr(self.primary, "minute_flag", "")

    @property
    def source_label(self) -> str:
        label = getattr(self.primary, "source_label", "主源")
        backup = getattr(self.secondary, "source_label", "备源")
        return f"{label}（失败回落 {backup}）"

    def _primary_ready(self) -> bool:
        return self._clock() >= self._blocked_until

    def trends(self, symbol: str, days: int | None = None):
        primary_error: DataSourceError | None = None
        if self._primary_ready():
            try:
                result = self.primary.trends(symbol, days)
                self.last_reason = ""
                self.last_source = getattr(result, "source", "") or "primary"
                return result
            except DataSourceError as exc:
                primary_error = exc
                self._blocked_until = self._clock() + self.cooldown
                self.last_reason = exc.message
                self.fallbacks += 1
            except Exception as exc:                     # 主源自身 bug 不该拖死分时
                primary_error = DataSourceError(
                    kind="internal",
                    message=f"{type(exc).__name__}: {exc}",
                    retryable=True,
                )
                self._blocked_until = self._clock()
                self.last_reason = primary_error.message
        else:
            self.last_reason = self.last_reason or "主源处于失败冷却期"

        try:
            result = self.secondary.trends(symbol, days)
        except DataSourceError as exc:
            if primary_error is not None:
                raise DataSourceError(
                    kind=exc.kind,
                    message=f"{exc.message}（主源同样失败：{primary_error.message}）",
                    retryable=exc.retryable,
                ) from exc
            raise
        self.last_source = getattr(result, "source", "") or "secondary"
        return result

    def batch(self, symbols: list[str], days: int | None = None):
        ok, bad = {}, {}
        for symbol in symbols:
            try:
                ok[symbol] = self.trends(symbol, days)
            except DataSourceError as exc:
                bad[symbol] = exc.message
        return ok, bad
