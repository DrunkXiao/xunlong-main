"""tqcenter 适配器（通达信 TdxQuant 本地数据服务）。

放置目标：`app/adapters/tqcenter_adapter.py`

已实测能力（tqcenter.py v1.0.3 / TPythClient.dll，2026-11 环境）：

| 能力 | 状态 | 说明 |
|---|---|---|
| 日/周/月 K 线 | 可用 | `get_market_data`，Volume 单位股、Amount 单位万元 |
| 分钟 K 线 | 不可用 | `1m/5m/15m/30m/1h` 均返回空 dict（本机无 minline 数据），`refresh_kline` 也补不出来 |
| 逐笔成交 | 不可用 | `period='tick'` 直接返回 `error=-5`；`subscribe_quote` 源码注释「暂无实际功能」 |
| 实时快照 | 可用 | `get_market_snapshot`，含五档、均价、内外盘、成交额；约 11ms/只 |
| 行情更新通知 | 可用 | `subscribe_hq` 回调只给 `{"Code":...,"ErrorId":"0"}`，**不含行情字段**，需反手拉快照；单次订阅上限 100 只 |
| 行业 | 可用 | `get_stock_info` 必须显式请求 `rs_hyname` / `rs_hycode_sim`，空 field_list 不返回行业 |
| 板块与成分股 | 部分 | `get_sector_list(1)` 569 个；`get_stock_list_in_sector` 部分板块成分异常（实测 880081 返回 2 只 ETF） |
| 自选股板块 | 不可用 | `get_user_sector()` 能列板块名，但 `get_stock_list_in_sector("1B")` 报 `server return none` |
| 龙虎榜 | 不可用 | 无对应接口，走 CSV 导入 |
| 财务/更多指标 | 可用 | `get_more_info` 提供 ZTPrice/DTPrice/fHSL/fLianB/Zsz/Ltsz/ZAF 系列 |

由此带来的硬约束（写入设计，不得绕过）：

1. **禁止把快照采样包装成逐笔**。分时线与分钟级"大单"只能由快照轮询差分得到，必须标
   `data_level='B'` 且 `quality_flag='sampled'`，界面显示「采样分时」而非「分时」。
2. **禁止把日 K 冒充分钟 K**。分钟周期返回空时抛 `DataSourceError(kind='unsupported')`，
   UI 置灰对应周期，不得用日线插值。
3. **必须本地预校验代码**。`get_stock_info` / `get_market_snapshot` / `get_market_data`
   内部在代码格式不合法时调用 `tq.close()`，会**关闭整个进程的连接**，因此本项目所有入口
   先过 `_valid_symbol()`。
4. **订阅上限 100 只**。超限抛错；大池轮询而非订阅。
"""

from __future__ import annotations

import atexit
import contextlib
import io
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.adapters.base import Capabilities, DataSourceError
from app.db.models import Bar, Quote

DEFAULT_USER_DIR = r"D:\new_tdx64\PYPlugins\user"
SYMBOL_RE = re.compile(r"^(6|0|3|4|8)\d{5}\.(SH|SZ|BJ)$")

# 本机实测：分钟线不可用，只有这四级可用
PERIOD_MAP = {"1d": "1d", "1w": "1w", "1M": "1mon"}
MINUTE_PERIODS = {"1m", "5m", "15m", "30m", "60m"}

SNAPSHOT_FIELDS = [
    "LastClose", "Open", "Max", "Min", "Now", "Volume", "NowVol", "Amount",
    "Inside", "Outside", "Average", "Buyp", "Buyv", "Sellp", "Sellv",
    "Before5MinNow", "TickDiff", "XsFlag",
]
MORE_FIELDS = [
    "ZTPrice", "DTPrice", "fHSL", "fLianB", "Zsz", "Ltsz", "ZAF",
    "ZAFPre5", "ZAFPre20", "Wtb", "Zjl", "L2TicNum", "L2OrderNum",
    "BCancel", "SCancel", "EverZTCount", "HqDate", "TotalBVol", "TotalSVol",
]
META_FIELDS = [
    "Name", "Unit", "MinPrice", "ActiveCapital", "IsSTGP", "IsQuitGP",
    "DelayMin", "rs_hyname", "rs_hycode_sim",
]


def _valid_symbol(symbol: str) -> bool:
    """本地校验代码，避免触发 tqcenter 内部的 tq.close()（它会关闭整条连接）。

    校验含市场/前缀一致性：`300001.SH` 这类前后缀冲突必须拦下，否则 get_stock_info
    会直接 close 连接，后续所有请求全部失败。
    """
    raw = str(symbol or "").strip().upper()
    match = SYMBOL_RE.match(raw)
    if not match:
        return False
    code, market = raw.split(".")
    if market == "SH":
        if code.startswith("000"):      # 000xxx.SH 为上证指数系列，合法
            return True
        return code[0] in "569"         # 6 股票 / 5 基金 / 9 B股
    if market == "SZ":
        if code.startswith("399"):      # 深证指数
            return True
        return code[0] in "0123"        # 0 主板 / 1 基金债 / 2 B股 / 3 创业板
    return code[0] in "48"              # BJ 北交所


def _f(value: Any, scale: float = 1.0) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value) * scale
    except (TypeError, ValueError):
        return None


def _list_floats(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value if v not in (None, "")]
    if isinstance(value, str):
        try:
            parsed = eval(value, {"__builtins__": {}})  # tq 返回的是 "[...]" 字面量字符串
        except Exception:
            return []
        if isinstance(parsed, list):
            return [float(v) for v in parsed if v not in (None, "")]
    return []


@dataclass(slots=True)
class OrderBook:
    symbol: str
    bids: list[tuple[float, int]] = field(default_factory=list)
    asks: list[tuple[float, int]] = field(default_factory=list)
    inside: int = 0
    outside: int = 0
    source: str = "tqcenter"
    data_level: str = "A"

    @property
    def bid1_amount(self) -> float | None:
        """买一挂单金额（元）。封单金额即此口径，不含撤单与排队。"""
        if not self.bids:
            return None
        price, volume = self.bids[0]
        return price * volume * 100


@dataclass(slots=True)
class SampledPoint:
    """快照差分得到的采样点。它不是逐笔，也不是交易所分时。"""
    symbol: str
    timestamp: datetime
    price: float
    average: float
    interval_volume: int      # 股
    interval_amount: float    # 元
    cumulative_volume: int
    cumulative_amount: float
    direction: str            # buy / sell / unknown（按内外盘增量推演）
    inferred: bool = True
    quality_flag: str = "sampled"


class SnapshotSampler:
    """把连续快照差分成分时点与分钟级成交，供分时图与"大单"使用。

    必须如实标注：这是采样推演，不是逐笔成交。单笔口径（几笔成交拼成一笔）永远拿不到，
    因此 tier 只能表示"区间累计成交额"，不能表示"某一笔成交金额"。
    """

    def __init__(self) -> None:
        self._last: dict[str, dict[str, Any]] = {}

    def feed(self, symbol: str, snap: dict, now: datetime | None = None) -> SampledPoint | None:
        now = now or datetime.now()
        price = _f(snap.get("Now"))
        average = _f(snap.get("Average"))
        if price is None:
            return None
        cum_volume = int(_f(snap.get("Volume"), 100) or 0)       # 手 -> 股
        cum_amount = (_f(snap.get("Amount"), 10000) or 0.0)      # 万元 -> 元
        inside = int(_f(snap.get("Inside"), 100) or 0)
        outside = int(_f(snap.get("Outside"), 100) or 0)

        prev = self._last.get(symbol)
        self._last[symbol] = {
            "volume": cum_volume, "amount": cum_amount,
            "inside": inside, "outside": outside, "price": price,
        }
        if prev is None:
            return None

        d_vol = max(0, cum_volume - prev["volume"])
        d_amt = max(0.0, cum_amount - prev["amount"])
        d_in = max(0, inside - prev["inside"])
        d_out = max(0, outside - prev["outside"])

        if d_out > d_in * 1.2:
            direction = "buy"
        elif d_in > d_out * 1.2:
            direction = "sell"
        else:
            direction = "unknown"

        return SampledPoint(
            symbol=symbol, timestamp=now, price=price,
            average=average if average is not None else price,
            interval_volume=d_vol, interval_amount=d_amt,
            cumulative_volume=cum_volume, cumulative_amount=cum_amount,
            direction=direction,
        )


class TqCenterAdapter:
    """MarketDataAdapter + TradeDataAdapter(降级) + IndustryDataAdapter 实现。"""

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or {}).get("datasource", {}) if config else {}
        self.user_dir = Path(cfg.get("user_dir") or DEFAULT_USER_DIR)
        self.strategy_name = cfg.get("strategy_name") or ""
        self.universe: list[str] = cfg.get("universe") or ["SH_MAIN", "SZ_MAIN", "CHINEXT"]
        self.timeout_ms = int(cfg.get("latency_abort_ms", 30000))
        self.more_ttl = float(cfg.get("more_cache_ttl_sec", 2.0))
        self._tq = None
        self._lock = threading.RLock()
        self._meta_cache: dict[str, dict] = {}
        self._more_cache: dict[str, tuple[float, dict]] = {}
        self._level = "A"
        self._tick_warned = False
        self._update_queue: list[str] = []
        self._subscribed: set[str] = set()
        self._sampler = SnapshotSampler()

    # ============================================================ 连接
    def connect(self) -> None:
        with self._lock:
            if self._tq is not None:
                return
            if not self.user_dir.exists():
                raise DataSourceError(
                    kind="config", retryable=False,
                    message=f"tqcenter 目录不存在: {self.user_dir}",
                )
            if str(self.user_dir) not in sys.path:
                sys.path.insert(0, str(self.user_dir))
            if not (self.user_dir.parent / "TPythClient.dll").exists():
                raise DataSourceError(
                    kind="config", retryable=False,
                    message=f"缺少 TPythClient.dll: {self.user_dir.parent}",
                )
            with self._quiet():
                from tqcenter import tq  # noqa: PLC0415  运行时导入
                anchor = self.strategy_name or os.path.join(
                    str(self.user_dir), f"_dragon_hunter_{os.getpid()}.py"
                )
                tq.initialize(anchor)
            self._tq = tq
            # 先于 tqcenter 的 atexit 执行，避免退出时刷 "TQ数据连接已关闭"
            atexit.register(self._silent_close)

    def disconnect(self) -> None:
        with self._lock:
            if self._tq is not None:
                with self._quiet():
                    try:
                        self._tq.close()
                    except Exception:
                        pass
                self._tq = None

    def _silent_close(self) -> None:
        try:
            if self._tq is not None:
                self._tq._initialized = False  # 让 tqcenter 自己的 atexit 静默
        except Exception:
            pass

    @contextlib.contextmanager
    def _quiet(self):
        """tqcenter 用 print 报错（字段不存在、取数失败），必须吞掉，异常另行判断。"""
        with self._lock, contextlib.redirect_stdout(io.StringIO()):
            yield

    def _call(self, name: str, *args, **kwargs):
        """统一出口：先确保连接，再吞掉 tqcenter 的 print，把异常转成 DataSourceError。

        name 为字符串（如 "get_market_data"），因为必须在 connect() 之后才能取到绑定方法。
        """
        self.connect()
        try:
            # "_tq_xxx" 是本适配器自己的包装方法，其余直接取 tqcenter 的类方法
            target = getattr(self, name) if name.startswith("_tq_") else getattr(self._tq, name)
        except AttributeError as exc:
            raise DataSourceError("api", f"tqcenter 无此方法: {name}", retryable=False) from exc
        try:
            with self._quiet():
                return target(*args, **kwargs)
        except DataSourceError:
            raise
        except Exception as exc:  # tqcenter 大量 raise ValueError("xxx异常")
            raise DataSourceError(
                kind="query", retryable=True,
                message=f"{name} 调用失败: {exc}",
            ) from exc

    # ============================================================ 能力
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            realtime=True,
            tick=False,
            order_book_levels=5,
            minute_kline=False,
            lhb=False,
            sector=True,
            subscribe_max=100,
            data_level=self._level,
            note="通达信客户端本地数据：日K/快照/五档/板块可用，逐笔与分钟K不可用",
        )

    def health_check(self) -> dict:
        start = time.time()
        try:
            snap = self._call("_tq_snapshot", "600000.SH")
            ok = bool(snap)
        except DataSourceError as exc:
            return {"connected": False, "latency_ms": None, "error": exc.message}
        return {
            "connected": ok,
            "latency_ms": int((time.time() - start) * 1000),
            "data_timestamp": datetime.now().isoformat(timespec="seconds"),
            "data_level": self._level,
        }

    # ============================================================ 基础
    def _tq_snapshot(self, symbol: str) -> dict:
        if not _valid_symbol(symbol):
            raise DataSourceError("symbol", f"非法代码: {symbol}", retryable=False)
        return self._tq.get_market_snapshot(symbol, field_list=SNAPSHOT_FIELDS) or {}

    def _tq_more(self, symbol: str) -> dict:
        """换手率/量比/涨跌停价来自 get_more_info，单只约 27ms，做短 TTL 缓存。
        轮询 12 只时未缓存需 ~550ms/轮，会造成 UI 卡顿。"""
        cached = self._more_cache.get(symbol)
        if cached and (time.time() - cached[0]) < self.more_ttl:
            return cached[1]
        if not _valid_symbol(symbol):
            raise DataSourceError("symbol", f"非法代码: {symbol}", retryable=False)
        more = self._tq.get_more_info(stock_code=symbol, field_list=MORE_FIELDS) or {}
        self._more_cache[symbol] = (time.time(), more)
        return more

    def _tq_meta(self, symbol: str) -> dict:
        cached = self._meta_cache.get(symbol)
        if cached:
            return cached
        if not _valid_symbol(symbol):
            raise DataSourceError("symbol", f"非法代码: {symbol}", retryable=False)
        self.connect()
        meta = self._tq.get_stock_info(stock_code=symbol, field_list=META_FIELDS) or {}
        if meta.get("DelayMin") is not None:
            self._level = "A" if int(float(meta["DelayMin"])) == 0 else "B"
        self._meta_cache[symbol] = meta
        return meta

    # ============================================================ 行情
    def get_stock_list(self) -> list[str]:
        codes = self._call("get_stock_list", market="5") or []
        return [c for c in codes if self._in_universe(c)]

    def _in_universe(self, symbol: str) -> bool:
        if not _valid_symbol(symbol):
            return False
        code, market = symbol.split(".")
        if market == "SH":
            main = code.startswith(("600", "601", "603", "605"))
            return main or ("SH_MAIN" in self.universe)
        if code.startswith(("000", "001", "002", "003")):
            return "SZ_MAIN" in self.universe
        if code.startswith(("300", "301")):
            return "CHINEXT" in self.universe
        return False

    def get_quote(self, symbol: str) -> Quote:
        snap = self._call("_tq_snapshot", symbol)
        if not snap:
            raise DataSourceError("empty", f"无快照数据: {symbol}", retryable=True)
        meta = self._tq_meta(symbol)
        more = self._call("_tq_more", symbol)
        now = datetime.now()
        price = _f(snap.get("Now"))
        prev_close = _f(snap.get("LastClose")) or _f(more.get("LastClose"))
        volume_hands = _f(snap.get("Volume"))
        return Quote(
            symbol=symbol,
            name=str(meta.get("Name", "")).replace("\u3000", " ").strip(),
            price=price,
            prev_close=prev_close,
            open=_f(snap.get("Open")),
            high=_f(snap.get("Max")),
            low=_f(snap.get("Min")),
            volume=int(volume_hands * 100) if volume_hands is not None else None,
            amount=(_f(snap.get("Amount"), 10000)),
            turnover_rate=_f(more.get("fHSL")),
            volume_ratio=_f(more.get("fLianB")),
            limit_up_price=_f(more.get("ZTPrice")),
            limit_down_price=_f(more.get("DTPrice")),
            data_level=self._level,
            source="tqcenter",
            quote_time=now,          # 快照不带行情时间戳，此处为本地接收时间
            received_at=now,
            time_source="local_receive",
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for symbol in symbols:
            if not _valid_symbol(symbol):
                continue
            try:
                out[symbol] = self.get_quote(symbol)
            except DataSourceError:
                continue
        return out

    def get_kline(self, symbol: str, period: str, start: datetime | None = None,
                  end: datetime | None = None, limit: int = 500) -> list[Bar]:
        if period in MINUTE_PERIODS:
            raise DataSourceError(
                kind="unsupported", retryable=False,
                message=f"tqcenter 本机分钟线返回空（period={period}），请改用日线或采样分时",
            )
        tq_period = PERIOD_MAP.get(period)
        if tq_period is None:
            raise DataSourceError("period", f"不支持的周期: {period}", retryable=False)
        if not _valid_symbol(symbol):
            raise DataSourceError("symbol", f"非法代码: {symbol}", retryable=False)

        kwargs: dict[str, Any] = {
            "field_list": ["Open", "High", "Low", "Close", "Volume", "Amount"],
            "stock_list": [symbol], "period": tq_period,
            "dividend_type": "none", "fill_data": False,
        }
        if start or end:
            kwargs["start_time"] = start.strftime("%Y%m%d%H%M%S") if start else ""
            kwargs["end_time"] = end.strftime("%Y%m%d%H%M%S") if end else ""
            kwargs["count"] = -1
        else:
            kwargs["count"] = limit
        data = self._call("get_market_data", **kwargs)
        if not isinstance(data, dict) or not data:
            raise DataSourceError("empty", f"K线为空: {symbol} {period}", retryable=True)
        return self._bars_from_frames(symbol, period, data)

    @staticmethod
    def _bars_from_frames(symbol: str, period: str, data: dict) -> list[Bar]:
        import pandas as pd  # noqa: PLC0415

        idx = None
        for frame in data.values():
            if isinstance(frame, pd.DataFrame) and symbol in frame.columns:
                idx = frame.index
                break
        if idx is None:
            return []

        def col(name: str, row) -> float | None:
            frame = data.get(name)
            if not isinstance(frame, pd.DataFrame) or symbol not in frame.columns:
                return None
            value = frame.loc[row, symbol]
            return None if pd.isna(value) else float(value)

        bars: list[Bar] = []
        for row in idx:
            close = col("Close", row)
            if close is None:  # fill_data=False 时停牌为 NaN，必须跳过而不是填 0
                continue
            stamp = row if isinstance(row, datetime) else pd.to_datetime(row).to_pydatetime()
            amount = col("Amount", row)
            bars.append(Bar(
                symbol=symbol, period=period, bar_time=stamp,
                open=col("Open", row) or close, high=col("High", row) or close,
                low=col("Low", row) or close, close=close,
                volume=int(col("Volume", row) or 0),
                amount=(amount or 0.0) * 10000,   # 万元 -> 元
                data_level="C", source="tqcenter",
            ))
        return bars

    # ============================================================ 盘口与采样
    def get_order_book(self, symbol: str) -> OrderBook:
        snap = self._call("_tq_snapshot", symbol)
        bids = list(zip(_list_floats(snap.get("Buyp")), _list_floats(snap.get("Buyv"))))
        asks = list(zip(_list_floats(snap.get("Sellp")), _list_floats(snap.get("Sellv"))))
        return OrderBook(
            symbol=symbol,
            bids=[(p, int(v)) for p, v in bids if p > 0],
            asks=[(p, int(v)) for p, v in asks if p > 0],
            inside=int(_f(snap.get("Inside"), 100) or 0),
            outside=int(_f(snap.get("Outside"), 100) or 0),
        )

    def sample_intraday(self, symbol: str, now: datetime | None = None) -> SampledPoint | None:
        """采样分时的唯一入口。返回 None 表示首帧（无差分基线）。"""
        snap = self._call("_tq_snapshot", symbol)
        point = self._sampler.feed(symbol, snap, now)
        if point is not None:
            point.quality_flag = "sampled"
        return point

    # ============================================================ 逐笔（明确不可用）
    def get_ticks(self, symbol: str, start: datetime | None = None,
                  end: datetime | None = None) -> list:
        """tqcenter 不提供逐笔成交。

        返回空列表而不是伪造数据；仅在首次调用时记录一条说明，避免刷日志。
        UI 应读取 capabilities.tick 决定是否渲染逐笔面板。
        """
        if not self._tick_warned:
            self._tick_warned = True
            print("[tqcenter] 该数据源不提供逐笔成交（period='tick' 返回 error=-5），"
                  "逐笔面板应显示不支持，分时改用 sample_intraday 采样")
        return []

    # ============================================================ 订阅
    def subscribe_quotes(self, symbols: list[str], callback=None) -> None:
        max_n = self.capabilities.subscribe_max
        new = [s for s in symbols if _valid_symbol(s) and s not in self._subscribed]
        if not new:
            return
        if len(self._subscribed) + len(new) > max_n:
            raise DataSourceError(
                kind="limit", retryable=False,
                message=f"tqcenter 订阅上限 {max_n} 只，当前 {len(self._subscribed)}，"
                        f"超出部分请改用快照轮询",
            )
        self.connect()

        def _cb(payload: str) -> None:
            # 回调只有 {"Code": "...", "ErrorId": "0"}，不含行情字段，需反手拉快照
            try:
                import json  # noqa: PLC0415
                code = json.loads(payload).get("Code")
                if code:
                    self._update_queue.append(code)
            except Exception:
                pass
            if callback is not None:
                callback(payload)

        self._call("subscribe_hq", stock_list=new, callback=_cb)
        self._subscribed.update(new)

    def unsubscribe_quotes(self, symbols: list[str]) -> None:
        target = [s for s in symbols if s in self._subscribed]
        if not target:
            return
        self._call("unsubscribe_hq", stock_list=target)
        self._subscribed.difference_update(target)

    def drain_updates(self) -> list[str]:
        """取出待刷新的代码（订阅通知队列）。"""
        out, self._update_queue = self._update_queue, []
        return out

    # ============================================================ 板块
    def get_sector_list(self) -> list[dict]:
        items = self._call("get_sector_list", 1) or []
        return [{"code": i.get("Code"), "name": i.get("Name")} for i in items]

    def get_sector_constituents(self, sector_code: str) -> list[str]:
        codes = self._call("get_stock_list_in_sector", sector_code) or []
        return [c for c in codes if _valid_symbol(c)]

    def get_industry(self, symbol: str) -> dict:
        meta = self._tq_meta(symbol)
        return {
            "industry_l1": meta.get("rs_hyname"),
            "industry_code": meta.get("rs_hycode_sim"),
            "source": "tqcenter",
        }

    def get_trading_dates(self, start: str, end: str, market: str = "SH") -> list[str]:
        return self._call("get_trading_dates", market, start, end) or []


def build_adapter(config: dict) -> TqCenterAdapter:
    return TqCenterAdapter(config)


if __name__ == "__main__":  # 自检：python tqcenter_adapter.py [symbol]
    import json

    symbol = sys.argv[1] if len(sys.argv) > 1 else "600000.SH"
    adapter = TqCenterAdapter()
    print("capabilities:", adapter.capabilities)
    print("health:", adapter.health_check())
    quote = adapter.get_quote(symbol)
    print("quote:", json.dumps({
        "name": quote.name, "price": quote.price, "pct": quote.change_pct,
        "amount_亿": round((quote.amount or 0) / 1e8, 2),
        "turnover": quote.turnover_rate, "vr": quote.volume_ratio,
        "zt": quote.limit_up_price, "level": quote.data_level,
    }, ensure_ascii=False))
    bars = adapter.get_kline(symbol, "1d", limit=5)
    print(f"kline 1d: {len(bars)} 根, 最后一根 {bars[-1].bar_time:%Y-%m-%d} "
          f"close={bars[-1].close} amount={bars[-1].amount/1e8:.2f}亿")
    ob = adapter.get_order_book(symbol)
    print(f"orderbook: bid1={ob.bids[:1]} ask1={ob.asks[:1]} 封单金额={ob.bid1_amount}")
    print("industry:", adapter.get_industry(symbol))
    try:
        adapter.get_kline(symbol, "5m", limit=5)
    except DataSourceError as exc:
        print("5m 预期不可用 ->", exc.kind, exc.message)
    adapter.disconnect()
