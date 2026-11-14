"""分时资金流（分钟级累计净额）数据源：东方财富 `fflow/kline`。

接口（与分时图同一只票、同一天，用 `secid` 对齐）：

    https://push2.eastmoney.com/api/qt/stock/fflow/kline/get
        ?klt=1&lmt=0
        &secid=1.600519
        &fields1=f1,f2,f3,f7
        &fields2=f51,f52,f53,f54,f55,f56

一行数据的字段顺序（固定，少一个就整行丢）：

    f51 时间 "2026-11-13 09:31"
    f52 主力净额    f53 小单净额    f54 中单净额
    f55 大单净额    f56 超大单净额

## 口径（实测确认，不是猜的）

- **这些数是「当日累计净额」，不是每分钟增量。** 09:31 从 0 起累加，15:00 就是
  当日全天净额。所以画图直接连线，**绝不能再累加一次**——再累加会得到一条
  越走越陡的假曲线，看起来像「资金持续加速流入」，实际只是把累计值又积了一遍。
- 单位是**元**，不需要换算（也不是万元、不是手）。
- `主力 = 超大单 + 大单`（600519 于 2026-11-13 实测：1.16 亿 + 0.57 亿 ≈ 1.57 亿，
  差 1 元为上游四舍五入）。本模块**只做映射，不重算、不互相顶替**：主力缺了就空着，
  不拿超大单+大单凑一条出来冒充。
- 序列从 **09:31** 开始（09:30 只有集合竞价，接口不给），共 240 点，终点 15:00。
- `rc=100` / `data=null` / `klines` 为空：停牌、退市或代码不支持，抛
  `DataSourceError(kind=not_found)`，**不返回空列表冒充「今天没有资金流」**——
  界面要能区分「数据没取到」和「这只票确实没有」。

## 为什么单独成一个适配器

日级资金流走 stockdb 本地库（`资金流` 表，见 money_flow.py），分时资金流走东财
公开接口，两者的新鲜度、失败原因、单位口径都不同。混在一个类里，副图报错时
用户分不清该去启动引擎还是该查网络。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime

from app.adapters.base import DataSourceError
# secid 的生成规则与分时图共用一份：两边算不一致，资金流就会取到另一只票
from app.adapters.eastmoney_intraday import secid

PATH = "/api/qt/stock/fflow/kline/get"
# 两个域名互为备份：push2 偶发直接断连（RemoteDisconnected），换 push2his 常常能拿到同一份数据
HOSTS = ("push2.eastmoney.com", "push2his.eastmoney.com")
PAGE_URL = "https://quote.eastmoney.com/"
SOURCE = "eastmoney:fflow"
DATA_LEVEL = "B"          # 公开接口的分钟聚合，非逐笔；与分时图同级
FIELDS1 = "f1,f2,f3,f7"
FIELDS2 = "f51,f52,f53,f54,f55,f56"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 五条曲线的字段名，UI 与测试都按这个顺序取，避免各处硬编码字符串
FLOW_FIELDS = ("main", "small", "mid", "big", "jumbo")


@dataclass(slots=True)
class MinuteFlowPoint:
    """某一分钟的累计净额（元）。字段缺失记 None，不补 0。"""

    time: datetime
    main: float | None = None       # 主力（超大单 + 大单）
    small: float | None = None      # 小单
    mid: float | None = None        # 中单
    big: float | None = None        # 大单
    jumbo: float | None = None      # 超大单


@dataclass(slots=True)
class MinuteFlowSeries:
    """某只票某一个交易日的分钟资金流（整日覆盖式）。"""

    symbol: str
    name: str = ""
    day: date | None = None
    points: list[MinuteFlowPoint] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    source: str = SOURCE
    data_level: str = DATA_LEVEL

    @property
    def latest(self) -> MinuteFlowPoint | None:
        return self.points[-1] if self.points else None

    def latest_values(self) -> dict[str, float | None]:
        """最新一分钟的五个净额，供图例显示「当前累计」。"""
        point = self.latest
        if point is None:
            return {key: None for key in FLOW_FIELDS}
        return {key: getattr(point, key, None) for key in FLOW_FIELDS}


def _num(value) -> float | None:
    """数值解析：'-'、空串、None 一律 None（上游用它们表示「这一档还没数」）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("-", "--"):
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _parse_time(text: str, fallback_day: date | None) -> datetime | None:
    """`2026-11-13 09:31` 优先；只有 `09:31` 时用上一行日期补全。

    接口目前给的是带日期的完整写法，但这里接受两种：一旦上游改成只回时分，
    整条资金流不该因此全空（这正是 WzSLinker 那套脚本踩过的坑）。
    """
    value = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    if fallback_day is not None and len(value) <= 5 and ":" in value:
        try:
            clock = datetime.strptime(value, "%H:%M").time()
        except ValueError:
            return None
        return datetime.combine(fallback_day, clock)
    return None


def points_from_klines(rows) -> list[MinuteFlowPoint]:
    """`klines` 字符串数组 → 按时间升序、同一分钟去重的点序列。

    脏行只丢自己：时间解析不出来、五档全是空值的行没有画图价值，但不该让
    整只票的资金流消失。五档里只要有一档有数就保留（其余为 None，画图断线）。
    """
    picked: dict[datetime, MinuteFlowPoint] = {}
    fallback_day: date | None = None
    for row in rows or []:
        parts = str(row).split(",")
        if len(parts) < 6:
            continue
        moment = _parse_time(parts[0], fallback_day)
        if moment is None:
            continue
        if fallback_day is None:
            fallback_day = moment.date()
        point = MinuteFlowPoint(
            time=moment,
            main=_num(parts[1]), small=_num(parts[2]), mid=_num(parts[3]),
            big=_num(parts[4]), jumbo=_num(parts[5]),
        )
        if all(getattr(point, key) is None for key in FLOW_FIELDS):
            continue
        picked[moment] = point
    return [picked[moment] for moment in sorted(picked)]


def _translate(exc: Exception) -> DataSourceError:
    text = f"{type(exc).__name__}: {exc}"
    return DataSourceError(
        kind="network",
        message=f"东方财富分时资金流接口不可达：{text}",
        retryable=True,
    )


class EastmoneyMoneyFlowAdapter:
    """分时页下方「资金流」副图的取数器。只读公开接口，不带账号信息。"""

    source = SOURCE
    data_level = DATA_LEVEL

    def __init__(self, config: dict | None = None) -> None:
        cfg = dict((config or {}).get("money_flow_intraday") or {})
        intraday = (config or {}).get("intraday") or {}
        self.enabled = bool(cfg.get("enabled", True))
        # 超时/重试缺省跟随 intraday 段：同一台机器访问同一个域名，
        # 分开配两套只会让「分时能取、资金流取不到」变成配置漂移
        self.timeout = float(cfg.get("timeout_sec", intraday.get("timeout_sec", 8)))
        self.retries = max(0, int(cfg.get("retries", intraday.get("retries", 1))))
        self.retry_sleep = float(cfg.get("retry_sleep_sec", intraday.get("retry_sleep_sec", 0.6)))
        self.cache_sec = max(0.0, float(cfg.get("cache_sec", 10)))
        # 代码 -> (取数时刻 monotonic, 序列)
        self._cache: dict[str, tuple[float, MinuteFlowSeries]] = {}

    # ---------------------------------------------------------------- HTTP
    def _request(self, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        last: Exception | None = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            host = HOSTS[attempt % len(HOSTS)]
            url = f"https://{host}{PATH}?{query}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": _UA,
                    "Referer": PAGE_URL,
                    "Accept": "application/json, text/plain, */*",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DataSourceError(
                        kind="bad_payload",
                        message=f"东方财富资金流返回非 JSON（前 80 字符：{raw[:80]}）",
                        retryable=False,
                    ) from exc
            except DataSourceError:
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < attempts - 1:
                    time.sleep(self.retry_sleep * (attempt + 1))
        raise _translate(last or RuntimeError("unknown"))

    # ---------------------------------------------------------------- 取数
    def series(self, symbol: str, force: bool = False) -> MinuteFlowSeries:
        """取当日分钟资金流。失败抛 DataSourceError，绝不返回空序列。

        `force=True` 跳过内存缓存（对应 F5 手动刷新）；盘中定时刷新不传 force，
        靠 `cache_sec < refresh_sec` 保证每次回填的都是新数。
        """
        if not self.enabled:
            raise DataSourceError(
                kind="disabled",
                message="分时资金流已关闭（config.yaml → money_flow_intraday.enabled: false）",
                retryable=False,
            )
        sid = secid(symbol)
        if not sid or sid.endswith("."):
            raise DataSourceError(
                kind="bad_request", message=f"无效代码：{symbol}", retryable=False)

        cached = self._cache.get(sid)
        if (not force and cached and self.cache_sec > 0
                and (time.monotonic() - cached[0]) < self.cache_sec):
            return cached[1]

        try:
            payload = self._request({
                "klt": 1,
                "lmt": 0,
                "secid": sid,
                "fields1": FIELDS1,
                "fields2": FIELDS2,
            })
        except DataSourceError:
            raise
        except Exception as exc:
            # 兜底：任何没被 _request 包成 DataSourceError 的异常（超时、SSL、
            # 未来换 HTTP 客户端）都要在这里定口径，否则会冒成后台线程的裸异常，
            # 界面只能收到一句「iflow 失败」，说不清是网络还是数据问题
            raise _translate(exc) from exc
        if payload.get("rc") not in (0, None):
            raise DataSourceError(
                kind="bad_request",
                message=(f"东方财富资金流拒绝请求：rc={payload.get('rc')}"
                         f"（{symbol} 可能不存在或已退市）"),
                retryable=False,
            )
        data = payload.get("data")
        if not data:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} 无分钟资金流（停牌、未上市或代码不属于沪深北）",
                retryable=False,
            )

        points = points_from_klines(data.get("klines") or [])
        if not points:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} 分钟资金流为空（当日可能全天停牌）",
                retryable=False,
            )
        # lmt=0 目前只回当天，但接口一旦改成多日，这里必须只留最后一个交易日，
        # 否则时间轴会把两天压进同一张 241 槽的图里
        last_day = points[-1].time.date()
        points = [p for p in points if p.time.date() == last_day]

        series = MinuteFlowSeries(
            symbol=symbol,
            name=str(data.get("name") or ""),
            day=last_day,
            points=points,
            fetched_at=datetime.now(),
        )
        self._cache[sid] = (time.monotonic(), series)
        return series

    def cached(self, symbol: str) -> MinuteFlowSeries | None:
        """内存里已有的序列（不看是否过期），切股回来时先画一版，不必等网络。"""
        entry = self._cache.get(secid(symbol))
        return entry[1] if entry else None

    def clear_cache(self) -> None:
        self._cache.clear()
