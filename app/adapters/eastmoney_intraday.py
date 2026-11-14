"""东方财富分时（分钟级）适配器 —— 用于替换「快照差分采样」的分时来源。

数据页：https://quote.eastmoney.com/sh600519.html （分时图）
接口：https://push2his.eastmoney.com/api/qt/stock/trends2/get

一行分钟数据的字段顺序（fields2=f51,f52,f53,f54,f55,f56,f57,f58）：

    f51 时间 "2026-11-06 09:30"
    f52 开   f53 收   f54 高   f55 低
    f56 成交量（**手**）  f57 成交额（元）  f58 均价（元）

口径与铁律：
- 这是**分钟聚合**，不是逐笔。1 分钟内的成交已被合并，方向无法从成交本身得知，
  只能由「本分钟收价 vs 上一分钟收价」推演，标 `price_proxy`，不冒充内外盘。
- 单位换算必须做：接口给的是**手**，本项目内部一律用**股**（×100）。金额是元，不用换。
- `f58` 是供应商给的均价（累计成交额/累计成交量口径），直接用，自己不重算后再显示，
  避免出现「均价和成交额对不上」的两套数。
- 开盘价可能是 0.00（`ndays>1` 时非首日第一根常见），所以价格一律取 `f53 收`。
- 停牌/不存在返回 `rc=100` + `data=null`，抛 `DataSourceError`，不返回空列表冒充「今天没成交」。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from app.adapters.base import DataSourceError
# 分钟线的公共结构：与 free-stockdb 适配器共用，两个来源交上去的形状必须一致
from app.adapters.minute import EM_FLAG, MinuteBar, Trends  # noqa: F401  (对外仍从这里导入)


BASE_URL = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
SOURCE = "eastmoney:trends"
DATA_LEVEL = "B"                     # 分钟聚合、非逐笔；与逐笔 A 级区分
PAGE_URL = "https://quote.eastmoney.com/"
TOTAL_SLOTS = 241

FIELDS1 = "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 交易所 → secid 前缀。北交所与深市共用 0。
MARKET_PREFIX = {"SH": "1", "SZ": "0", "BJ": "0"}


def secid(symbol: str) -> str:
    """`600519.SH` → `1.600519`；`002696.SZ` / `920002.BJ` → `0.xxxxxx`。"""
    text = (symbol or "").strip().upper()
    if "." in text:
        code, market = text.split(".", 1)
        prefix = MARKET_PREFIX.get(market)
        if prefix is None:
            prefix = "1" if code.startswith(("6", "9", "5")) else "0"
        return f"{prefix}.{code}"
    # 没有后缀时按首位推断：6/9/5 开头归沪市，其余归深市
    return f"{'1' if text.startswith(('6', '9', '5')) else '0'}.{text}"


def _float(value) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class EastmoneyIntradayAdapter:
    """分时取数器。只读公开接口，不带账号信息。"""

    source_label = "东方财富 1 分钟"

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or {}).get("intraday") or {}
        self.timeout = float(cfg.get("timeout_sec", 10))
        self.retries = int(cfg.get("retries", 2))
        self.retry_sleep = float(cfg.get("retry_sleep_sec", 0.6))
        self.days = max(1, min(5, int(cfg.get("days", 1))))   # 接口上限 5 天
        self.source = SOURCE
        self.data_level = DATA_LEVEL

    # ---------------------------------------------------------------- HTTP
    def _request(self, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}?{query}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": _UA,
                    "Referer": PAGE_URL,
                    "Accept": "application/json, text/plain, */*",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise DataSourceError(
                        kind="bad_payload",
                        message=f"东方财富分时返回非 JSON（前 80 字符：{raw[:80]}）",
                        retryable=False,
                    ) from exc
                if payload.get("rc") not in (0, None):
                    raise DataSourceError(
                        kind="bad_request",
                        message=f"东方财富分时拒绝请求：rc={payload.get('rc')} "
                                f"（代码可能不存在或已退市）",
                        retryable=False,
                    )
                return payload
            except DataSourceError:
                raise
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(self.retry_sleep * (attempt + 1))
        raise DataSourceError(
            kind="network",
            message=f"东方财富分时接口不可达：{type(last).__name__}: {last}",
            retryable=True,
        )

    # ---------------------------------------------------------------- 取数
    def trends(self, symbol: str, days: int | None = None) -> Trends:
        """取整日分钟序列。无数据（停牌/退市）抛 DataSourceError(kind=not_found)。"""
        n = max(1, min(5, int(days or self.days)))
        payload = self._request({
            "secid": secid(symbol),
            "fields1": FIELDS1,
            "fields2": FIELDS2,
            "iscr": 0,
            "ndays": n,
        })
        data = payload.get("data")
        if not data:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} 无分时数据（可能停牌、未上市或代码不属于沪深北）",
                retryable=False,
            )
        bars = _parse_rows(data.get("trends") or [])
        if not bars:
            raise DataSourceError(
                kind="not_found",
                message=f"{symbol} 分时序列为空（当日可能全天停牌）",
                retryable=False,
            )
        # ndays>1 时只保留最后一个交易日，避免把历史分钟混进当天分时
        last_day = bars[-1].time.date()
        bars = [b for b in bars if b.time.date() == last_day]
        return Trends(
            symbol=symbol,
            name=str(data.get("name") or ""),
            pre_close=_float(data.get("preClose")),
            day=last_day,
            bars=bars,
            fetched_at=datetime.now(),
            source=SOURCE,
            data_level=DATA_LEVEL,
            minute_flag=EM_FLAG,
        )

    def batch(self, symbols: list[str], days: int | None = None) -> tuple[dict[str, Trends], dict[str, str]]:
        """顺序取多只，返回 (成功, 失败原因)。单只失败不影响其它。"""
        ok: dict[str, Trends] = {}
        bad: dict[str, str] = {}
        for symbol in symbols:
            try:
                ok[symbol] = self.trends(symbol, days)
            except DataSourceError as exc:
                bad[symbol] = exc.message
        return ok, bad


def _parse_rows(rows: list[str]) -> list[MinuteBar]:
    out: list[MinuteBar] = []
    for row in rows:
        parts = str(row).split(",")
        if len(parts) < 8:
            continue
        try:
            moment = datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            continue
        close = _float(parts[2])
        if close is None or close <= 0:
            continue
        open_ = _float(parts[1]) or close
        high = _float(parts[3]) or close
        low = _float(parts[4]) or close
        hand = _float(parts[5]) or 0.0
        amount = _float(parts[6]) or 0.0
        volume = int(round(hand * 100))     # 手 → 股
        if volume <= 0:
            # 零成交分钟是真实存在的（比如尾盘前某分钟无撮合），保留但量额为 0
            volume = 0
            amount = 0.0
        out.append(MinuteBar(
            time=moment, open=open_, close=close, high=high, low=low,
            volume=volume, amount=amount, avg_price=_float(parts[7]),
            volume_hand=int(round(hand)),
        ))
    out.sort(key=lambda b: b.time)
    return out
