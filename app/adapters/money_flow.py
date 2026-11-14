"""资金流（主力净额）数据源：stockdb 本地库的 `资金流` 表。

## 表结构（rd 分层 K-V）

    资金流 : code : YYYYMMDD  ->  {
        code, date,
        main_net,                    # 主力净额（元）
        jumbo_net,                   # 超大单净额
        big_net,                     # 大单净额
        mid_net,                     # 中单净额
        small_net,                   # 小单净额
        main_in / main_out,          # 主力买入额 / 卖出额
        retail_in / retail_out,      # 散户买入额 / 卖出额
    }

## 单位与口径（实测确认，不是猜的）

单位一律是**元**，不是万元、不是手。600519 在 20261113 的真实记录：

    main_net    =  287,884,467
    jumbo_net   =  301,860,752
    big_net     =  -13,976,286
    jumbo+big   =  287,884,466      ← 与 main_net 差 1 元，是入库四舍五入
    main_in-out = 3,666,371,159 - 3,378,486,693 = 287,884,466   ← 同口径

所以 `main_net` 就是「主力净额」= 超大单 + 大单，**不要再自己相加**，
也不要再除以 1e4 当成万元（界面上显示时用 _fmt_yuan 转成「亿/万」即可）。

## 读取方式（按接口文档第 4/5/6 节）

    rd.vals("资金流", code, "*")[-n:]

`*` 是整层匹配（文档 5.2），`[-n:]` 会写成请求参数 `num=-n`（文档 6.3），
由服务端只回最近 n 个交易日。不能在 Python 里把整只票的历史全量下载再切片
（600519 已有 1766 条，全市场逐股这么干会把本地引擎打满）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime

from app.adapters import sdk_bootstrap
from app.adapters.base import DataSourceError
from app.adapters.free_stockdb_intraday import stockdb_code

SOURCE = "stockdb:moneyflow"
TABLE = "资金流"
DATA_LEVEL = "C"          # 本地同步落盘，与分钟线同级
PAGE_LIMIT = 500          # 单次最多回多少条，防止配置写大了把一次请求变成全量


@dataclass(slots=True)
class MoneyFlowRow:
    """一个交易日的资金流。金额单位：元。"""

    day: date
    main_net: float = 0.0        # 主力净额（超大单 + 大单）
    jumbo_net: float = 0.0       # 超大单净额
    big_net: float = 0.0         # 大单净额
    mid_net: float = 0.0         # 中单净额
    small_net: float = 0.0       # 小单净额

    @property
    def key(self) -> str:
        """与 K 线 `bar_time` 的对齐键（同一种 `YYYY-MM-DD` 写法）。"""
        return self.day.strftime("%Y-%m-%d")


def _as_float(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_day(value) -> date | None:
    """`20261113`（int/str）→ date；8 位以外一律判非法，宁可丢一天也不猜日期。"""
    text = str(value if not isinstance(value, float) else int(value)).strip()
    if not text.isdigit() or len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def rows_from_records(records) -> list[MoneyFlowRow]:
    """引擎原始记录 → MoneyFlowRow（按日期升序、同日去重后写覆盖）。

    脏数据只丢当天，不影响整段：`date` 非法或金额全空的记录没有画图价值，
    但一个异常字段不该让整只票的资金流消失。
    """
    picked: dict[date, MoneyFlowRow] = {}
    for item in records or []:
        if not isinstance(item, dict):
            continue
        day = parse_day(item.get("date"))
        if day is None:
            continue
        main_net = _as_float(item.get("main_net"))
        if main_net is None:
            # 主力净额是这张图的唯一数据，缺了就跳过；其它单类净额为 None 才补 0
            continue
        picked[day] = MoneyFlowRow(
            day=day,
            main_net=main_net,
            jumbo_net=_as_float(item.get("jumbo_net")) or 0.0,
            big_net=_as_float(item.get("big_net")) or 0.0,
            mid_net=_as_float(item.get("mid_net")) or 0.0,
            small_net=_as_float(item.get("small_net")) or 0.0,
        )
    return [picked[day] for day in sorted(picked)]


def _translate(exc: Exception, code: str) -> DataSourceError:
    """底层异常 → 可操作的 DataSourceError。

    两种失败必须分开：引擎没起是 `network`（去启动 stockdb.exe），
    协议/回包不对是 `bad_payload`（去对版本）。混成一句「取数失败」，
    用户就不知道该修哪一头。
    """
    text = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return DataSourceError(
            kind="network",
            message=(f"free-stockdb 引擎不可达，资金流取不到（{code}）：{text}。"
                     "请先启动 stockdb.exe，并确认数据已更新到当日"),
            retryable=True,
        )
    return DataSourceError(
        kind="bad_payload",
        message=f"读取资金流失败（{code}）：{text}",
        retryable=False,
    )


class SdkReader:
    """`rd` 的薄封装：只负责把「一层表 + 代码」翻成记录列表。

    单独成类是为了可注入假 reader —— 测试不该要求本机装着 stockdb 引擎。
    """

    def __init__(self, sdk_dir: str = "", table: str = TABLE) -> None:
        self.sdk_dir = str(sdk_dir or "").strip()
        self.table = table or TABLE
        self.loaded_from = ""

    def records(self, code: str, limit: int) -> list[dict]:
        """读最近 `limit` 个交易日的原始记录。失败一律抛 DataSourceError。"""
        rd = sdk_bootstrap.load(self.sdk_dir)
        self.loaded_from = sdk_bootstrap.loaded_dir()
        try:
            result = rd.vals(self.table, code, "*")[-limit:].do()
        except DataSourceError:
            raise
        except Exception as exc:          # 引擎没起、pyd 版本不符、协议错误……
            raise _translate(exc, code) from exc
        if result is None:
            return []
        if not isinstance(result, list):
            raise DataSourceError(
                kind="bad_payload",
                message=f"资金流回包不是列表（{code}）：{type(result).__name__}",
                retryable=False,
            )
        return [row for row in result if isinstance(row, dict)]


class MoneyFlowAdapter:
    """日K 下方「主力净额」副图的数据源。

    与分时适配器不同，这里没有回落：资金流只有 stockdb 一家能给，
    拿不到就如实显示「无数据 + 原因」，绝不拿价格推算一个假的净额出来。
    """

    source = SOURCE
    data_level = DATA_LEVEL

    def __init__(self, config: dict | None = None, reader: SdkReader | None = None) -> None:
        cfg = dict((config or {}).get("money_flow") or {})
        sdk_dir = str(((((config or {}).get("intraday") or {})
                        .get("free_stockdb") or {}).get("sdk_dir") or "")).strip()
        self.enabled = bool(cfg.get("enabled", True))
        self.days = max(5, min(PAGE_LIMIT, int(cfg.get("days", 120))))
        self.cache_sec = max(0.0, float(cfg.get("cache_sec", 120)))
        self.table = str(cfg.get("table") or TABLE)
        self.reader = reader or SdkReader(sdk_dir, self.table)
        # code -> (取数时刻, 记录, 上次请求的条数)
        self._cache: dict[str, tuple[float, list[MoneyFlowRow], int]] = {}
        self.last_error = ""

    # ---------------------------------------------------------------- 取数
    def series(self, symbol: str, days: int | None = None) -> list[MoneyFlowRow]:
        """按日期升序返回最近若干交易日的资金流（含主力净额）。

        空列表 = 该股在库中确实没有资金流记录；取数失败抛 DataSourceError。
        这两种情况必须分开：前者是数据没同步，后者是引擎没起，
        混成一个空图的话用户根本不知道该去修什么。
        """
        if not self.enabled:
            raise DataSourceError(
                kind="disabled",
                message="资金流已关闭（config.yaml → money_flow.enabled: false）",
                retryable=False,
            )
        code = stockdb_code(symbol)
        if not code:
            raise DataSourceError(
                kind="bad_request", message=f"无效代码：{symbol}", retryable=False)

        limit = max(5, min(PAGE_LIMIT, int(days or self.days)))
        cached = self._cache.get(code)
        now = time.monotonic()
        # 只有「上次取的条数不少于这次要的」才能复用：要更长的历史就重新取，
        # 不拿一段短的冒充完整序列。
        if (cached and self.cache_sec > 0 and (now - cached[0]) < self.cache_sec
                and limit <= cached[2]):
            return cached[1][-limit:]

        try:
            records = self.reader.records(code, limit)
        except DataSourceError:
            raise
        except Exception as exc:      # 注入的 reader 或未来换源：契约仍是 DataSourceError
            raise _translate(exc, code) from exc
        rows = rows_from_records(records)
        if not rows:
            self.last_error = (f"{code} 在 `{self.table}` 表里没有记录："
                               "可能是新股、退市，或本地数据还没同步到该股")
            raise DataSourceError(
                kind="not_found", message=self.last_error, retryable=False)

        self.last_error = ""
        self._cache[code] = (now, rows, limit)
        return rows[-limit:]

    def latest(self, symbol: str) -> MoneyFlowRow | None:
        """最近一个交易日的主力净额；没有数据时返回 None（不抛）。"""
        try:
            rows = self.series(symbol, days=5)
        except DataSourceError:
            return None
        return rows[-1] if rows else None

    def cached(self, symbol: str) -> list[MoneyFlowRow]:
        """内存里已有的序列（不看是否过期）。没有就返回空列表。

        用途：切到周K再切回日K时，副图不必重新发请求即可立刻恢复。
        """
        entry = self._cache.get(stockdb_code(symbol))
        return list(entry[1]) if entry else []
    def clear_cache(self) -> None:
        self._cache.clear()
