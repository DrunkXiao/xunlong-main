"""free-stockdb 分时（1 分钟）适配器 —— 走官方 Python SDK（`stock_sdk.rd`）。

数据引擎：https://github.com/hello245m/free-stockdb
Python 文档：http://a.123128.xyz/docs/AI策略python开发接口文档.md

## 为什么必须用 SDK，而不是自己拼 HTTP

`stockdb.exe` 监听 `127.0.0.1:7899`，但它的回包是**二进制打包协议**，不是 JSON。
实测 `GET /?cmd=get&t=分钟k:600519:20261113*` 拿到的是一堆二进制块（解码后是乱码），
按 JSON 解析必然 `JSONDecodeError`。上一版适配器就是这么写的，结果每轮都抛
`bad_payload` → 分时页永远空白。文档第 2 节说得很明确：Python 侧唯一入口是

    from stock_sdk import rd, bk, zb

`rd` 负责把二进制协议解成 Python dict，我们只关心 dict 的字段。

## 取数口径（全部来自官方文档，不是猜的）

    # 当天全部分钟（分钟K 查整天：start/end 传相同的 8 位日期）
    rd.get_data("600519", start="20261113", end="20261113", frequency="1m", fq=None)

    # 最新一根分钟（定位「最近交易日」，一次请求搞定，不用逐日回溯）
    rd.keys("分钟k", "600519", "*")[-1:].do()

    # 日线（顺带拿到 name 与 pre_close，不用自己推昨收）
    rd.get_data("600519", start="20261001", end="20261113", frequency="1d", fq=None)

一条分钟记录（`rd.get_data` 返回的 dict）：

    {'code': '600519', 'date': 20261113093000,
     'open': 1404.0, 'close': 1404.0, 'high': 1404.0, 'low': 1404.0,
     'volume': 15000, 'amount': 21060000}

- `date` 是 **int 型 14 位** `YYYYMMDDHHMMSS`，不是字符串。
- `volume` 单位是**股**（15000 × 1404 = 21060000，与 `amount` 对得上）。
  若引擎版本给的是手，配置 `volume_unit: hand` 会 ×100。
- 没有均价字段：本适配器用「当日累计额 / 当日累计量」递推 VWAP，
  与东财 `f58 均价` 同口径，换源后分时黄线含义不变。
- `fq=None` 是必须的：分钟线一旦复权，价格就和盘口/最新价对不上，
  分时图会整体偏移。文档默认 `qfq` 只适合日线策略，不适合分时展示。

## 与东财源的差异

1. 本地落盘 + 增量同步，不是推送流。盘中能否看到今天的分钟，取决于用户
   有没有跑数据更新工具。所以 `data_level` 标 **C**，默认 `strict_today: true`：
   盘中最新一分钟不是今天 → 抛 `stale`，让界面显示「当日分钟未同步」，
   而不是把昨天的分时冒充成今天。
2. 不需要网络鉴权、不限频，15 秒拉一次毫无压力。
3. 全市场都在同一个本地库里，9 宫格不会打满任何外部接口。

## 异常约定

全部转成 `DataSourceError`，由 `IntradayPoller` 统一处理（记错误、退避、保留旧序列）：

- `not_installed`：没装/没找到 `stock_sdk`（`sdk_dir` 指错、或 pybao 没拷过来）
- `network`：SDK 连不上 `stockdb.exe`（服务没启动）
- `bad_payload`：SDK 抛了非网络异常，或返回结构不认识
- `not_found`：该代码在库里没有分钟数据（未同步 / 退市 / 代码不在库中）
- `stale`：有数据但不是当天（盘中）
- `bad_request`：代码非法
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from app.adapters import sdk_bootstrap
from app.adapters.base import DataSourceError
from app.adapters.minute import STOCKDB_FLAG, MinuteBar, Trends

SOURCE = "stockdb:minute"
DATA_LEVEL = "C"                     # 本地同步落盘，非实时授权行情
MINUTE_FLAG = STOCKDB_FLAG
TABLE_MINUTE = "分钟k"
TABLE_DAILY = "日k"

# 盘中判定：集合竞价开始到收盘后 5 分钟，都要求「今天必须有分钟」
LIVE_START = (9, 15)
LIVE_END = (15, 5)

# 兼容旧引用：真正的候选目录在 sdk_bootstrap.DEFAULT_SDK_DIRS
DEFAULT_SDK_DIRS = sdk_bootstrap.DEFAULT_SDK_DIRS


def stockdb_code(symbol: str) -> str:
    """`600519.SH` → `600519`；`sh600519` / `600519` 原样归一。"""
    text = (symbol or "").strip().upper()
    if "." in text:
        text = text.split(".", 1)[0]
    for prefix in ("SH", "SZ", "BJ"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text.strip("_ ")


def parse_minute_time(value) -> datetime | None:
    """14 位 `20260625145200`（int 或 str）→ datetime。

    官方示例里同时出现过 13 位（`2026062310000`，少一位），那是文档笔误；
    这里 12/13/14 位都按「补到秒」处理，其余一律判为非法，宁可丢一根也不猜时间。
    """
    if isinstance(value, float):
        value = int(value)
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    if len(text) == 13:
        text += "0"
    if len(text) == 12:
        text += "00"
    if len(text) != 14:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def day_from_key(key: str) -> date | None:
    """`分钟k:600519:20261113150000` → date(2026, 11, 13)。"""
    tail = str(key or "").rsplit(":", 1)[-1]
    moment = parse_minute_time(tail)
    return moment.date() if moment else None


def is_live(now: datetime | None = None) -> bool:
    """是否处于「今天必须有分钟线」的时段（工作日 09:15–15:05）。"""
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    return LIVE_START <= (now.hour, now.minute) <= LIVE_END


def _as_float(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


class SdkReader:
    """`stock_sdk.rd` 的薄封装：只做取数，不含任何业务判断。

    真正的「找目录 / 加 sys.path / import / 缓存」都在 `sdk_bootstrap` 里，
    因为那件事必须发生在 PySide6 加载之前（Qt 的导入钩子会让 `stockdb.pyd`
    抛 wrapper loop），而适配器是在 Qt 之后才被构造的。
    这里只是把它取出来用，顺便让测试可以注入假 reader。
    """

    def __init__(self, sdk_dir: str | Path | None = None) -> None:
        self.sdk_dir = str(sdk_dir or "").strip()
        self._lock = threading.Lock()
        self._rd = None
        self.loaded_from = ""

    def module(self):
        """返回 `stock_sdk.rd`；失败抛 `DataSourceError`（not_installed / qt_conflict）。"""
        with self._lock:
            if self._rd is not None:
                return self._rd
            rd = sdk_bootstrap.load(self.sdk_dir or None)
            self._rd = rd
            self.loaded_from = sdk_bootstrap.loaded_dir()
            return rd

    # ---------------------------------------------------------------- 调用
    def _guard(self, action, what: str):
        rd_ready = self.module()
        try:
            return action(rd_ready)
        except DataSourceError:
            raise
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise DataSourceError(
                kind="network",
                message=(f"free-stockdb 引擎不可达（{what}）：{type(exc).__name__}: {exc}。"
                         "请先启动 stockdb.exe 并确认 127.0.0.1:7899 在监听"),
                retryable=True,
            ) from exc
        except Exception as exc:                     # SDK 自身协议/数据异常
            raise DataSourceError(
                kind="bad_payload",
                message=f"free-stockdb 查询失败（{what}）：{type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc

    def latest_minute_key(self, code: str) -> str:
        """该代码库里最新一根分钟线的完整键；没有数据返回空串。"""
        def action(rd):
            keys = rd.keys(TABLE_MINUTE, code, "*")[-1:].do()
            return str(keys[0]) if keys else ""
        return self._guard(action, f"{TABLE_MINUTE}:{code}:最新一根")

    def minute_rows(self, code: str, day: date) -> list[dict]:
        """某天的整日分钟记录（start/end 传相同 8 位日期＝查整天）。"""
        text = f"{day:%Y%m%d}"

        def action(rd):
            return rd.get_data(code, start=text, end=text, frequency="1m", fq=None)

        rows = self._guard(action, f"{code} {text} 1m")
        if not rows:                                  # get_data 不认时退回原生 KV 前缀查询
            def raw(rd):
                return rd.vals(TABLE_MINUTE, code, f"{text}*").do()
            rows = self._guard(raw, f"{TABLE_MINUTE}:{code}:{text}*")
        return [row for row in rows or [] if isinstance(row, dict)]

    def daily_rows(self, code: str, start: date, end: date) -> list[dict]:
        """日线（用来补 name / pre_close）。"""
        def action(rd):
            return rd.get_data(code, start=f"{start:%Y%m%d}", end=f"{end:%Y%m%d}",
                               frequency="1d", fq=None)
        rows = self._guard(action, f"{code} 日线")
        return [row for row in rows or [] if isinstance(row, dict)]

    def code_groups(self) -> dict:
        """`股票代码` 表：`{'0': [...], '6': [...]}`，用于探活。"""
        def action(rd):
            return rd.get("股票代码").do()
        result = self._guard(action, "股票代码")
        return result if isinstance(result, dict) else {}


class FreeStockDbIntradayAdapter:
    """读本机 free-stockdb 的 `分钟k` 表，产出与东财源同形状的 `Trends`。"""

    minute_flag = MINUTE_FLAG
    source_label = "free-stockdb 本地库 1 分钟"

    def __init__(self, config: dict | None = None, reader=None) -> None:
        cfg = ((config or {}).get("intraday") or {}).get("free_stockdb") or {}
        self.host = str(cfg.get("host", "127.0.0.1"))
        self.port = int(cfg.get("port", 7899))
        self.base_url = f"http://{self.host}:{self.port}"
        self.sdk_dir = str(cfg.get("sdk_dir") or "")
        self.password = str(cfg.get("password") or "")
        self.table = str(cfg.get("table") or TABLE_MINUTE)
        self.backfill_days = max(1, min(10, int(cfg.get("days", 5))))
        self.strict_today = bool(cfg.get("strict_today", True))
        self.volume_in_hand = str(cfg.get("volume_unit", "share")).lower() == "hand"
        self.source = SOURCE
        self.data_level = DATA_LEVEL
        self.reader = reader if reader is not None else SdkReader(self.sdk_dir)
        self._daily_cache: dict[str, tuple[str, float | None, date | None]] = {}

    # ---------------------------------------------------------------- 探活
    def health(self) -> dict:
        """轻量探活：能取到股票代码表就认为 SDK + 引擎都正常。"""
        groups = self.reader.code_groups()
        codes = sum(len(v) for v in groups.values() if isinstance(v, (list, tuple)))
        return {
            "ok": codes > 0,
            "base_url": self.base_url,
            "table": self.table,
            "codes": codes,
            "sdk_dir": getattr(self.reader, "loaded_from", "") or self.sdk_dir,
        }

    # ---------------------------------------------------------------- 取数
    def _daily_context(self, code: str, day: date) -> tuple[str, float | None]:
        """从 `日k` 补 (名称, 昨收)。取不到就返回 ("", None)，不让它拖垮分时。

        优先直接用日线自带的 `pre_close` 字段（官方文档确认存在），
        没有才退回「取目标日之前最后一根收盘价」，两者都不猜。
        """
        cached = self._daily_cache.get(code)
        if cached and cached[2] == day:
            return cached[0], cached[1]
        name, pre_close = "", None
        try:
            rows = self.reader.daily_rows(code, day - timedelta(days=30), day)
        except DataSourceError:
            rows = []
        target = int(f"{day:%Y%m%d}")
        prior: list[tuple[int, float]] = []
        for row in rows:
            stamp = _as_int(str(row.get("date") or "")[:8])
            if not stamp:
                continue
            if row.get("name"):
                name = str(row["name"])
            if stamp == target:
                pre_close = _as_float(row.get("pre_close"))
            close = _as_float(row.get("close"))
            if close and stamp < target:
                prior.append((stamp, close))
        if pre_close is None and prior:
            pre_close = max(prior)[1]
        self._daily_cache[code] = (name, pre_close, day)
        return name, pre_close

    def latest_day(self, code: str, days: int | None = None) -> date | None:
        """最近一个有分钟数据的交易日。

        先问「最新一根分钟键」（一次请求），引擎不支持时再逐日回溯 `days` 天。
        注意：`latest_minute_key` 的失败不能直接吞掉——若逐日回溯一次都没真正发出去
        （比如周六日全被跳过），必须把「引擎连不上」如实抛上去，否则会被误报成
        「这只票没数据」，用户就会去查数据同步而不是查服务有没有起。
        """
        primary_error: DataSourceError | None = None
        try:
            key = self.reader.latest_minute_key(code)
        except DataSourceError as exc:
            primary_error = exc
            key = ""
        found = day_from_key(key)
        if found is not None:
            return found

        span = max(1, min(10, int(days or self.backfill_days)))
        today = date.today()
        probed = False
        for offset in range(span):
            probe = today - timedelta(days=offset)
            if probe.weekday() >= 5:        # 周末库里不会有分钟，少发一次请求
                continue
            probed = True
            if self.reader.minute_rows(code, probe):
                return probe
        if primary_error is not None and not probed:
            raise primary_error
        return None

    def trends(self, symbol: str, days: int | None = None) -> Trends:
        """取「最近一个有分钟数据的交易日」的整日分钟序列。"""
        code = stockdb_code(symbol)
        if not code:
            raise DataSourceError(kind="bad_request", message=f"非法代码：{symbol!r}",
                                  retryable=False)
        day = self.latest_day(code, days)
        if day is None:
            span = max(1, min(10, int(days or self.backfill_days)))
            raise DataSourceError(
                kind="not_found",
                message=(f"{symbol} 在 free-stockdb 的 {self.table} 里没有最近 {span} 天的分钟数据"
                         f"（未同步 / 已退市 / 代码不在库中）。请先运行数据更新工具"),
                retryable=False,
            )

        today = date.today()
        if self.strict_today and is_live() and day != today:
            raise DataSourceError(
                kind="stale",
                message=(f"free-stockdb 当日分钟尚未同步（最新 {day:%Y-%m-%d}，"
                         f"今天是 {today:%Y-%m-%d}）。请运行数据更新工具后重试"),
                retryable=True,
            )

        rows = self.reader.minute_rows(code, day)
        if not rows:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} {day:%Y-%m-%d} 没有分钟记录（键存在但内容为空）",
                retryable=False,
            )

        bars = self._to_bars(rows, day)
        if not bars:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} {day:%Y-%m-%d} 的分钟记录全部非法（缺 OHLC），已丢弃",
                retryable=False,
            )

        name, pre_close = self._daily_context(code, day)
        return Trends(
            symbol=symbol,
            name=name,
            pre_close=pre_close,
            day=day,
            bars=bars,
            fetched_at=datetime.now(),
            source=SOURCE,
            data_level=DATA_LEVEL,
            minute_flag=MINUTE_FLAG,
        )

    def _to_bars(self, rows: list[dict], day: date) -> list[MinuteBar]:
        """记录 → MinuteBar。累计量额递推均价，与东财 f58 同口径。"""
        by_time: dict[datetime, dict] = {}
        for row in rows:
            moment = parse_minute_time(row.get("date"))
            if moment is None or moment.date() != day:
                continue
            close = _as_float(row.get("close"))
            if not close or close <= 0:
                continue
            by_time[moment] = row          # 同一分钟重复出现只留最后一条
        if not by_time:
            return []

        bars: list[MinuteBar] = []
        cum_volume = 0
        cum_amount = 0.0
        for moment in sorted(by_time):
            row = by_time[moment]
            close = _as_float(row.get("close")) or 0.0
            open_ = _as_float(row.get("open")) or close
            high = _as_float(row.get("high")) or close
            low = _as_float(row.get("low")) or close
            raw_volume = _as_float(row.get("volume")) or 0.0
            volume = int(round(raw_volume * (100 if self.volume_in_hand else 1)))
            amount = _as_float(row.get("amount")) or 0.0
            if volume <= 0:
                # 零成交分钟真实存在（该分钟无撮合），保留但量额为 0，标 no_trade 由上层做
                volume, amount = 0, 0.0
            cum_volume += volume
            cum_amount += amount
            bars.append(MinuteBar(
                time=moment, open=open_, close=close, high=high, low=low,
                volume=volume, amount=amount,
                avg_price=(cum_amount / cum_volume) if cum_volume else None,
                volume_hand=int(round(volume / 100)) if not self.volume_in_hand
                else int(round(raw_volume)),
            ))
        return bars

    def batch(self, symbols: list[str], days: int | None = None
              ) -> tuple[dict[str, Trends], dict[str, str]]:
        """顺序取多只，返回 (成功, 失败原因)。单只失败不影响其它。"""
        ok: dict[str, Trends] = {}
        bad: dict[str, str] = {}
        for symbol in symbols:
            try:
                ok[symbol] = self.trends(symbol, days)
            except DataSourceError as exc:
                bad[symbol] = exc.message
        return ok, bad
