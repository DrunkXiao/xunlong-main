#!/usr/bin/env python
"""寻龙盯盘 Dragon Hunter — 工程骨架生成器.

用法:
    python scaffold_project.py <target_dir> [--force]

生成的骨架严格遵循 SKILL.md 的十条铁律：
- 数据带 source / data_level / timestamp
- 红涨绿跌主题常量集中在 app/ui/theme.py
- 分组与布局落 SQLite（schema 来自 assets/db/schema.sql）
- 数据源经 Adapter 协议装配，默认 mock(D 级)
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_SRC = SKILL_ROOT / "assets" / "db" / "schema.sql"
THEME_SRC = SKILL_ROOT / "assets" / "ui" / "theme.css"
REQ_SRC = SKILL_ROOT / "assets" / "templates" / "requirements.txt"
CONFIG_SRC = SKILL_ROOT / "assets" / "templates" / "config.example.yaml"
ADAPTER_SRC = SKILL_ROOT / "assets" / "adapters" / "tqcenter_adapter.py"

FILES: dict[str, str] = {}

FILES["app/__init__.py"] = '''"""寻龙盯盘 Dragon Hunter."""
__version__ = "0.1.0"
'''

FILES["app/config.py"] = '''"""配置加载：config.yaml -> dict，缺文件时回落到安全默认值。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "app": {"name": "寻龙盯盘", "data_dir": "./data", "log_level": "INFO"},
    "database": {"url": "sqlite:///./data/dragon_hunter.db"},
    "datasource": {
        "name": "tqcenter",
        "data_level": "A",
        "poll_interval_sec": 3,
        "latency_warn_ms": 5000,
        "latency_abort_ms": 30000,
        "user_dir": r"D:/new_tdx64/PYPlugins/user",
        "strategy_name": "",
        "universe": ["SH_MAIN", "SZ_MAIN", "CHINEXT"],
        "more_cache_ttl_sec": 2.0,
        "enable_tick": False,
        "enable_minute_kline": False,
        "enable_lhb": False,
    },
    "market": {"heartbeat_sec": 5, "calendar_source": "approximate"},
    "large_order": {
        "tiers": {"normal": 1000000, "large": 3000000, "super": 5000000, "continuous": 10000000},
        "streak_count": 3,
        "streak_amount_multiplier": 2,
    },
    "alerts": {"default_cooldown_sec": 300, "per_symbol_per_minute_limit": 5},
    "tdx_bridge": {"enabled": False, "default_group": "盘中异动池", "http_port": 8611},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = "config.yaml") -> dict[str, Any]:
    """读取 YAML 配置。文件缺失或 PyYAML 未安装时返回默认值，不抛异常。"""
    config = dict(DEFAULTS)
    if path is None:
        return config
    file = Path(path)
    if not file.exists():
        return config
    try:
        import yaml  # 延迟导入，缺依赖时仍可启动
    except ImportError:
        return config
    with file.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if isinstance(loaded, dict):
        config = _deep_merge(config, loaded)
    return config


def sqlite_path_from_url(url: str) -> Path:
    """从 sqlite:///./data/x.db 解析出文件路径。"""
    if not url.startswith("sqlite:///"):
        raise ValueError(f"仅支持 sqlite:/// URL，收到: {url}")
    raw = url[len("sqlite:///"):]
    return Path(raw)
'''

FILES["app/db/__init__.py"] = '''from .session import Database

__all__ = ["Database"]
'''

FILES["app/db/session.py"] = '''"""SQLite 访问层：统一连接、建表、备份。"""
from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA_FILENAME = "schema.sql"


class Database:
    def __init__(self, db_path: str | Path, schema_path: str | Path | None = None) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if schema_path is None:
            schema_path = Path(__file__).with_name(SCHEMA_FILENAME)
        self.schema_path = Path(schema_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        conn = self.connect()
        try:
            yield conn.cursor()
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> int:
        """执行 schema.sql，返回表数量。重复执行为幂等。"""
        if not self.schema_path.exists():
            raise FileNotFoundError(f"缺少建表脚本: {self.schema_path}")
        sql = self.schema_path.read_text(encoding="utf-8")
        with self.cursor() as cur:
            cur.executescript(sql)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return len(cur.fetchall())

    def seed_default_groups(self, names: list[str] | None = None) -> None:
        """写入默认分组（模块H），已存在则跳过。"""
        groups = names or [
            "龙头核心池", "市场最高板", "连板晋级池", "首板观察池", "二波启动池",
            "强势趋势池", "主线题材池", "龙虎榜观察池", "盘中异动池", "待复盘股票池",
        ]
        with self.cursor() as cur:
            for idx, name in enumerate(groups):
                cur.execute(
                    "INSERT OR IGNORE INTO watch_groups (name, sort_order, is_default) VALUES (?, ?, 1)",
                    (name, idx),
                )

    def backup(self, keep: int = 5) -> Path:
        """本地数据库备份（铁律：必须支持备份恢复）。"""
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{self.path.stem}_{stamp}.db"
        shutil.copy2(self.path, target)
        backups = sorted(backup_dir.glob(f"{self.path.stem}_*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return target

    def table_names(self) -> list[str]:
        with self.cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return [row["name"] for row in cur.fetchall()]
'''

FILES["app/db/models.py"] = '''"""领域数据结构。所有行情对象必须携带 source / data_level / timestamp。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

DATA_LEVELS = ("A", "B", "C", "D")
LEVEL_LABEL = {
    "A": "实时（授权）",
    "B": "延时",
    "C": "历史",
    "D": "模拟数据",
}


@dataclass(slots=True)
class Quote:
    symbol: str
    name: str
    price: float | None = None
    prev_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    amount: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    limit_up_price: float | None = None
    limit_down_price: float | None = None
    data_level: str = "D"
    source: str = ""
    quote_time: datetime | None = None
    received_at: datetime | None = None
    time_source: str = "exchange"   # exchange 交易所时间 / local_receive 本地接收时间

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return (self.price - self.prev_close) / self.prev_close * 100

    @property
    def latency_ms(self) -> int | None:
        if self.quote_time is None or self.received_at is None:
            return None
        return int((self.received_at - self.quote_time).total_seconds() * 1000)


@dataclass(slots=True)
class Bar:
    symbol: str
    period: str
    bar_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    amount: float = 0.0
    data_level: str = "C"
    source: str = ""


@dataclass(slots=True)
class Tick:
    symbol: str
    trade_time: datetime
    price: float
    volume: int
    amount: float
    direction: str = "unknown"  # buy / sell / unknown
    seq: int = 0
    data_level: str = "C"
    source: str = ""
    quality_flag: str = "ok"


@dataclass(slots=True)
class AlertEvent:
    symbol: str
    name: str
    event_type: str
    trigger_price: float
    trigger_time: datetime
    condition: str
    level: str = "P3"
    volume: int | None = None
    amount: float | None = None
    data_level: str = "C"
    source: str = ""
    handled: int = 0
    extra: dict = field(default_factory=dict)
'''

FILES["app/adapters/__init__.py"] = '''from .base import (
    DataSourceError,
    Capabilities,
    MarketDataAdapter,
    TradeDataAdapter,
    IndustryDataAdapter,
    DragonTigerAdapter,
)
from .mock import MockAdapter

__all__ = [
    "DataSourceError",
    "Capabilities",
    "MarketDataAdapter",
    "TradeDataAdapter",
    "IndustryDataAdapter",
    "DragonTigerAdapter",
    "MockAdapter",
    "build_adapter",
]


def build_adapter(config: dict):
    """按配置装配适配器。新增供应商时只改这里，UI 与服务层不动。"""
    name = (config.get("datasource") or {}).get("name", "tqcenter")
    if name == "mock":
        return MockAdapter()
    if name == "tqcenter":
        from app.adapters.tqcenter_adapter import TqCenterAdapter  # 延迟导入，避免强依赖
        return TqCenterAdapter(config)
    raise DataSourceError(
        kind="not_implemented",
        message=f"数据源 {name} 尚未实现，请在 app/adapters/ 下新增适配器后在 build_adapter 注册",
        retryable=False,
    )
'''

FILES["app/adapters/base.py"] = '''"""适配器协议：业务层只依赖这些接口，禁止硬编码供应商。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.db.models import Bar, Quote, Tick


@dataclass(slots=True)
class Capabilities:
    """数据源能力矩阵。UI 据此决定是否渲染面板，而不渲染空面板。"""
    realtime: bool = False
    tick: bool = False
    order_book_levels: int = 0
    minute_kline: bool = False
    lhb: bool = False
    sector: bool = False
    subscribe_max: int = 0
    data_level: str = "D"
    note: str = ""

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, feature, False))

    def summary(self) -> str:
        items = [
            f"实时={'是' if self.realtime else '否'}",
            f"逐笔={'是' if self.tick else '否'}",
            f"盘口={self.order_book_levels}档",
            f"分钟K={'是' if self.minute_kline else '否'}",
            f"龙虎榜={'是' if self.lhb else '否'}",
        ]
        if self.subscribe_max:
            items.append(f"订阅上限={self.subscribe_max}")
        return " | ".join(items)


class DataSourceError(Exception):
    def __init__(self, kind: str, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.retryable = retryable


@runtime_checkable
class MarketDataAdapter(Protocol):
    @property
    def capabilities(self) -> Capabilities: ...
    def get_stock_list(self) -> list[Quote]: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def get_kline(self, symbol: str, period: str, start: datetime | None = None,
                  end: datetime | None = None, limit: int = 500) -> list[Bar]: ...
    async def subscribe_quotes(self, symbols: list[str]) -> None: ...
    async def unsubscribe_quotes(self, symbols: list[str]) -> None: ...


@runtime_checkable
class TradeDataAdapter(Protocol):
    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]: ...
    async def subscribe_ticks(self, symbol: str) -> None: ...
    def get_order_book(self, symbol: str): ...


@runtime_checkable
class IndustryDataAdapter(Protocol):
    def get_industry(self, symbol: str) -> dict: ...
    def get_concepts(self, symbol: str) -> list[str]: ...
    def get_sector_quotes(self, sector: str) -> dict: ...
    def get_sector_constituents(self, sector: str) -> list[str]: ...


@runtime_checkable
class DragonTigerAdapter(Protocol):
    def get_lhb_records(self, symbol: str, start, end) -> list[dict]: ...
    def get_lhb_detail(self, trade_date, symbol: str) -> dict | None: ...
'''

FILES["app/adapters/mock.py"] = '''"""D 级模拟数据源。仅用于开发联调，界面必须显示「模拟数据」标识。"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from app.adapters.base import Capabilities
from app.db.models import Bar, Quote, Tick

MOCK_UNIVERSE = [
    ("600000.SH", "示例银行"),
    ("600519.SH", "示例白酒"),
    ("000001.SZ", "示例股份"),
    ("300001.SZ", "示例创业"),
    ("002594.SZ", "示例汽车"),
    ("601318.SH", "示例保险"),
]


class MockAdapter:
    data_level = "D"

    def __init__(self, seed: int = 20261108) -> None:
        self._rng = random.Random(seed)
        self._subscribed: set[str] = set()

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            realtime=False,
            tick=True,
            order_book_levels=0,
            data_level="D",
            note="模拟数据，不可用于任何交易决策",
        )

    def get_stock_list(self) -> list[str]:
        """返回代码列表（与 tqcenter 适配器保持同一返回类型）。"""
        return [symbol for symbol, _ in MOCK_UNIVERSE]

    def get_quote(self, symbol: str) -> Quote:
        name = dict(MOCK_UNIVERSE).get(symbol, "未知标的")
        base = 8 + (abs(hash(symbol)) % 4000) / 100
        now = datetime.now()
        price = round(base * (1 + self._rng.uniform(-0.03, 0.03)), 2)
        prev_close = round(base, 2)
        return Quote(
            symbol=symbol,
            name=name,
            price=price,
            prev_close=prev_close,
            open=round(prev_close * 1.005, 2),
            high=round(max(price, prev_close) * 1.01, 2),
            low=round(min(price, prev_close) * 0.99, 2),
            volume=self._rng.randint(100_000, 9_000_000),
            amount=round(self._rng.uniform(5e7, 2e9), 2),
            turnover_rate=round(self._rng.uniform(0.3, 9.0), 2),
            volume_ratio=round(self._rng.uniform(0.5, 4.0), 2),
            limit_up_price=round(prev_close * 1.1, 2),
            limit_down_price=round(prev_close * 0.9, 2),
            data_level="D",
            source="mock",
            quote_time=now,
            received_at=now,
        )

    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {symbol: self.get_quote(symbol) for symbol in symbols}

    def get_kline(self, symbol: str, period: str, start: datetime | None = None,
                  end: datetime | None = None, limit: int = 500) -> list[Bar]:
        bars: list[Bar] = []
        price = 10.0
        start_time = datetime.now() - timedelta(days=limit)
        for i in range(limit):
            open_ = price
            close = round(open_ * (1 + self._rng.uniform(-0.04, 0.04)), 2)
            bars.append(Bar(
                symbol=symbol, period=period,
                bar_time=start_time + timedelta(days=i),
                open=open_, high=round(max(open_, close) * 1.01, 2),
                low=round(min(open_, close) * 0.99, 2), close=close,
                volume=self._rng.randint(10_000, 900_000),
                data_level="D", source="mock",
            ))
            price = close
        return bars

    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]:
        ticks: list[Tick] = []
        cursor = start
        seq = 0
        while cursor <= end:
            seq += 1
            price = round(self._rng.uniform(9.5, 10.5), 2)
            volume = self._rng.choice([100, 200, 500, 1000, 2000, 5000, 20000])
            ticks.append(Tick(
                symbol=symbol, trade_time=cursor, price=price, volume=volume,
                amount=round(price * volume, 2),
                direction=self._rng.choice(["buy", "sell", "unknown"]),
                seq=seq, data_level="D", source="mock",
            ))
            cursor += timedelta(milliseconds=self._rng.randint(300, 3000))
        return ticks

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        self._subscribed.update(symbols)

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        self._subscribed.difference_update(symbols)
'''

FILES["app/services/__init__.py"] = '''"""业务服务层。"""
'''

FILES["app/services/large_order.py"] = '''"""大单识别（模块D）。只描述成交金额、方向与价格位置，禁止推断资金身份。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.db.models import Tick

DEFAULT_TIERS = {
    "normal": 1_000_000,
    "large": 3_000_000,
    "super": 5_000_000,
    "continuous": 10_000_000,
}

TIER_LABEL = {
    "normal": "普通大单",
    "large": "大额成交",
    "super": "超大额成交",
    "continuous": "连续异动成交",
}

ALLOWED_TAGS = ("买入活跃", "卖出活跃", "买卖分歧", "成交放大", "量价背离", "资金行为不明确")


@dataclass(slots=True)
class LargeOrder:
    symbol: str
    tier: str
    direction: str
    price: float
    volume: int
    amount: float
    event_time: datetime
    tag: str = "资金行为不明确"
    price_position: str = "mid"


def classify_tier(amount: float, tiers: dict[str, float] | None = None) -> str | None:
    """金额 >= 最低档阈值时返回档位，否则 None。"""
    thresholds = dict(DEFAULT_TIERS)
    if tiers:
        thresholds.update(tiers)
    if amount >= thresholds["continuous"]:
        return "continuous"
    if amount >= thresholds["super"]:
        return "super"
    if amount >= thresholds["large"]:
        return "large"
    if amount >= thresholds["normal"]:
        return "normal"
    return None


def price_position(price: float, recent_low: float, recent_high: float) -> str:
    """近 N 日区间分位：>=70% 高位，<=30% 低位。"""
    if recent_high <= recent_low:
        return "mid"
    pos = (price - recent_low) / (recent_high - recent_low)
    if pos >= 0.7:
        return "relative_high"
    if pos <= 0.3:
        return "relative_low"
    return "mid"


def tag_direction(buy_amount: float, sell_amount: float) -> str:
    """方向标签。只做金额对比，不做身份判断。"""
    total = buy_amount + sell_amount
    if total <= 0:
        return "资金行为不明确"
    ratio = buy_amount / total
    if ratio >= 0.7:
        return "买入活跃"
    if ratio <= 0.3:
        return "卖出活跃"
    return "买卖分歧"


def detect_streak(ticks: list[Tick], count: int = 3, min_amount: float = 2_000_000) -> list[LargeOrder]:
    """连续同向主动成交：连续 count 笔同向且累计金额达标。"""
    events: list[LargeOrder] = []
    streak_dir: str | None = None
    streak: list[Tick] = []
    for tick in ticks:
        if tick.direction == "unknown":
            streak_dir, streak = None, []
            continue
        if tick.direction == streak_dir:
            streak.append(tick)
        else:
            streak_dir, streak = tick.direction, [tick]
        if len(streak) >= count:
            total = sum(t.amount for t in streak)
            if total >= min_amount:
                events.append(LargeOrder(
                    symbol=tick.symbol, tier="continuous", direction=tick.direction,
                    price=tick.price, volume=sum(t.volume for t in streak),
                    amount=round(total, 2), event_time=tick.trade_time,
                    tag="买入活跃" if tick.direction == "buy" else "卖出活跃",
                ))
                streak_dir, streak = None, []
    return events
'''

FILES["app/services/alert_engine.py"] = '''"""预警引擎：条件匹配 -> 去重冷却 -> 通知 -> 落库。"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from app.db.models import AlertEvent, Quote

LEVEL_NOTIFY = {
    "P1": ["popup", "sound", "log"],
    "P2": ["popup", "sound", "log"],
    "P3": ["log"],
    "P4": ["log"],
}


class AlertEngine:
    def __init__(self, cooldown_sec: int = 300, per_symbol_per_minute: int = 5) -> None:
        self.cooldown_sec = cooldown_sec
        self.per_symbol_per_minute = per_symbol_per_minute
        self._last_fired: dict[tuple[str, str, str], tuple[datetime, float]] = {}
        self._minute_count: dict[tuple[str, str], int] = {}

    # ---------------------------------------------------------------- 去重
    def should_fire(self, symbol: str, rule_id: str, event_type: str,
                    price: float, now: datetime) -> bool:
        key = (str(rule_id), symbol, event_type)
        last = self._last_fired.get(key)
        if last:
            last_time, last_price = last
            if (now - last_time).total_seconds() < self.cooldown_sec:
                return False
            # 同值抑制：价格几乎未变视为同一事件延续
            if last_price and abs(price - last_price) <= max(0.01, last_price * 0.001):
                return False
        minute_key = (symbol, now.strftime("%Y%m%d%H%M"))
        if self._minute_count.get(minute_key, 0) >= self.per_symbol_per_minute:
            return False
        self._last_fired[key] = (now, price)
        self._minute_count[minute_key] = self._minute_count.get(minute_key, 0) + 1
        return True

    # ---------------------------------------------------------------- 规则
    def check_price_rules(self, quote: Quote, now: datetime | None = None) -> list[AlertEvent]:
        now = now or datetime.now()
        events: list[AlertEvent] = []
        if quote.price is None or quote.data_level == "D":
            return events

        if quote.limit_up_price:
            gap = (quote.limit_up_price - quote.price) / quote.limit_up_price * 100
            if 0 <= gap <= 1.0 and self.should_fire(quote.symbol, "near_limit_up", "near_limit_up", quote.price, now):
                events.append(AlertEvent(
                    symbol=quote.symbol, name=quote.name, event_type="near_limit_up",
                    trigger_price=quote.price, trigger_time=now,
                    condition=f"distance_to_limit_up={gap:.2f}%", level="P2",
                    data_level=quote.data_level, source=quote.source,
                ))
        if quote.limit_down_price:
            gap = (quote.price - quote.limit_down_price) / quote.limit_down_price * 100
            if 0 <= gap <= 1.0 and self.should_fire(quote.symbol, "near_limit_down", "near_limit_down", quote.price, now):
                events.append(AlertEvent(
                    symbol=quote.symbol, name=quote.name, event_type="near_limit_down",
                    trigger_price=quote.price, trigger_time=now,
                    condition=f"distance_to_limit_down={gap:.2f}%", level="P2",
                    data_level=quote.data_level, source=quote.source,
                ))
        return events

    # ---------------------------------------------------------------- 落库
    @staticmethod
    def persist(cur: sqlite3.Cursor, event: AlertEvent) -> None:
        cur.execute(
            """INSERT INTO alert_events
               (symbol, name, event_type, trigger_price, trigger_time, condition,
                volume, amount, data_level, source, level, handled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event.symbol, event.name, event.event_type, event.trigger_price,
             event.trigger_time.isoformat(timespec="seconds"), event.condition,
             event.volume, event.amount, event.data_level, event.source,
             event.level, event.handled),
        )


def format_message(event: AlertEvent) -> str:
    """通知文案：只陈述事实，不含买卖建议。"""
    return (f"{event.name} {event.event_type}"
            f"（{event.trigger_price:.2f}，{event.trigger_time.strftime('%H:%M:%S')}）")
'''

FILES["app/services/tdx_bridge.py"] = '''"""通达信桥接（模块G）：代码标准化、去重、落库、记录事件。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

CODE_RE = re.compile(r"^(\\d{6})(?:\\.(SH|SZ|BJ))?$")
SUPPORTED_PREFIX = {
    "5": "SH", "6": "SH", "9": "SH",
    "0": "SZ", "1": "SZ", "2": "SZ", "3": "SZ",
    "4": "BJ", "8": "BJ",
}


def _infer_market(digits: str) -> str | None:
    return SUPPORTED_PREFIX.get(digits[0])


def normalize_symbol(code: str) -> str | None:
    """6 位代码转标准 symbol。前缀与后缀冲突、或超出支持范围返回 None。"""
    raw = str(code).strip().upper()
    match = CODE_RE.match(raw)
    if not match:
        return None
    digits, suffix = match.group(1), match.group(2)
    inferred = _infer_market(digits)
    if inferred is None:
        return None
    if suffix and suffix != inferred:
        return None
    return f"{digits}.{inferred}"


def parse_payload(payload: dict) -> list[dict]:
    """解析桥接事件，返回 [{symbol, name, group}]。"""
    out: list[dict] = []
    for item in payload.get("symbols", []):
        symbol = normalize_symbol(item.get("code", ""))
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": item.get("name", ""),
            "group": item.get("group") or payload.get("group") or "盘中异动池",
        })
    return out


def ingest(cur: sqlite3.Cursor, payload: dict, channel: str = "json_watch") -> dict:
    """导入一批股票，返回统计。重复自动忽略。"""
    items = parse_payload(payload)
    now = datetime.now()
    new_count = 0
    for item in items:
        cur.execute(
            "INSERT OR IGNORE INTO watch_groups (name, sort_order) VALUES (?, 999)",
            (item["group"],),
        )
        cur.execute("SELECT id FROM watch_groups WHERE name = ?", (item["group"],))
        row = cur.fetchone()
        if row is None:
            continue
        group_id = row[0]
        cur.execute(
            "INSERT OR IGNORE INTO stock_info (symbol, code, name, market, source) VALUES (?, ?, ?, ?, ?)",
            (item["symbol"], item["symbol"].split(".")[0], item["name"] or "未命名",
             item["symbol"].split(".")[1], "tongdaxin"),
        )
        cur.execute(
            "INSERT OR IGNORE INTO watch_group_stocks (group_id, symbol, sort_order) VALUES (?, ?, 999)",
            (group_id, item["symbol"]),
        )
        if cur.rowcount:
            new_count += 1

    cur.execute(
        """INSERT INTO tdx_events
           (event, channel, group_name, raw_payload, symbol_cnt, new_cnt, event_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (payload.get("event", "unknown"), channel, payload.get("group"),
         json.dumps(payload, ensure_ascii=False), len(items), new_count,
         now.isoformat(timespec="seconds")),
    )
    return {"total": len(items), "new": new_count}


def read_csv(path: str | Path, group: str) -> dict:
    """CSV 通道：每行 code[,name]。"""
    symbols = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0] or not parts[0][0].isdigit():
            continue
        symbols.append({"code": parts[0], "name": parts[1] if len(parts) > 1 else "", "group": group})
    return {"source": "tongdaxin", "event": "watchlist_update",
            "timestamp": datetime.now().isoformat(timespec="seconds"), "symbols": symbols}
'''

FILES["app/api/__init__.py"] = '''"""本地 HTTP / WebSocket 接口层。"""
'''

FILES["app/api/app.py"] = '''"""FastAPI 应用：供桌面端与外部桥接调用。"""
from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.adapters import build_adapter
from app.config import load_config, sqlite_path_from_url
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.tdx_bridge import ingest, read_csv

app = FastAPI(title="寻龙盯盘 本地服务", version="0.1.0")
config = load_config()
adapter = build_adapter(config)
engine = AlertEngine()


def _database() -> Database:
    path = sqlite_path_from_url(config["database"]["url"])
    return Database(path)


def _ingest_with_db(payload: dict, channel: str) -> dict:
    database = _database()
    with database.cursor() as cur:
        return ingest(cur, payload, channel=channel)


@app.get("/health")
def health() -> dict:
    caps = adapter.capabilities
    return {
        "status": "ok",
        "datasource": config.get("datasource", {}).get("name"),
        "data_level": caps.data_level,
        "realtime": caps.realtime,
        "tick": caps.tick,
        "order_book_levels": caps.order_book_levels,
        "note": caps.note,
    }


@app.get("/api/quotes")
def quotes(symbols: str = "") -> list[dict]:
    wanted = [s for s in symbols.split(",") if s] or [q.symbol for q in adapter.get_stock_list()]
    return [
        {
            "symbol": q.symbol, "name": q.name, "price": q.price,
            "change_pct": q.change_pct, "amount": q.amount,
            "data_level": q.data_level, "source": q.source,
            "quote_time": q.quote_time.isoformat() if q.quote_time else None,
        }
        for q in adapter.get_quotes(wanted).values()
    ]


@app.post("/api/tdx/ingest")
def tdx_ingest(payload: dict) -> dict:
    """通达信桥接 HTTP 通道：写入分组、去重、记录 tdx_events。"""
    return _ingest_with_db(payload, channel="http")


@app.post("/api/tdx/csv")
def tdx_csv(path: str, group: str = "盘中异动池") -> dict:
    """通达信 CSV 通道：导入导出文件。"""
    return _ingest_with_db(read_csv(path, group), channel="csv")


@app.websocket("/ws/quotes")
async def ws_quotes(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            symbols = (await ws.receive_text()).split(",")
            await ws.send_json([
                {"symbol": q.symbol, "price": q.price, "change_pct": q.change_pct,
                 "data_level": q.data_level}
                for q in adapter.get_quotes([s for s in symbols if s]).values()
            ])
    except WebSocketDisconnect:
        return
'''

FILES["app/ui/__init__.py"] = '''"""PySide6 桌面界面。"""
'''

FILES["app/ui/theme.py"] = '''"""暗色交易主题常量。A股惯例：红涨绿跌，禁止欧美配色。"""
from __future__ import annotations

from pathlib import Path

COLORS = {
    "bg": "#0B0E11",
    "panel": "#12161C",
    "raised": "#1A1F27",
    "border": "#232A34",
    "text": "#E6EAF0",
    "text_dim": "#9AA4B2",
    "text_muted": "#5C6673",
    "up": "#F04438",
    "down": "#12B76A",
    "warn": "#F5B544",
    "info": "#3D8BFD",
    "unknown": "#5C6673",
}

MONO_FONT = '"JetBrains Mono", "Consolas", monospace'
UI_FONT = '"Microsoft YaHei UI", "Segoe UI", sans-serif'


def color_for_change(value: float | None) -> str:
    """涨红、跌绿、平灰。"""
    if value is None:
        return COLORS["unknown"]
    if value > 0:
        return COLORS["up"]
    if value < 0:
        return COLORS["down"]
    return COLORS["text_dim"]


def load_qss() -> str:
    qss = Path(__file__).with_name("theme.css")
    return qss.read_text(encoding="utf-8") if qss.exists() else ""


def format_amount(value: float | None) -> str:
    """中文单位：万 / 亿。无数据返回 '--'，不显示 0。"""
    if value is None:
        return "--"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.2f}万"
    return f"{value:.0f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}%"
'''

FILES["app/ui/main_window.py"] = '''"""主窗口骨架：左分组 / 中图表占位 / 右面板 / 底部日志 / 顶栏状态。"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QSplitter, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from app.adapters import build_adapter
from app.adapters.base import DataSourceError
from app.config import load_config
from app.services.alert_engine import AlertEngine, format_message
from app.ui.theme import COLORS, color_for_change, format_amount, format_pct, load_qss

DEFAULT_GROUPS = [
    "龙头核心池", "市场最高板", "连板晋级池", "首板观察池", "二波启动池",
    "强势趋势池", "主线题材池", "龙虎榜观察池", "盘中异动池", "待复盘股票池",
]

COLUMNS = ["名称", "代码", "最新价", "涨跌幅", "成交额", "换手率", "量比", "数据等级"]


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.config = load_config()
        self.adapter = build_adapter(self.config)
        self.engine = AlertEngine()
        self.setWindowTitle("寻龙盯盘 Dragon Hunter")
        self.resize(1600, 960)
        self.setStyleSheet(load_qss())
        self._build()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(int(self.config.get("datasource", {}).get("poll_interval_sec", 3)) * 1000)

    # ------------------------------------------------------------ 布局
    def _build(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)
        root.addWidget(self._build_topbar())

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self._build_left())
        self.splitter.addWidget(self._build_center())
        self.splitter.addWidget(self._build_right())
        self.splitter.setSizes([260, 980, 340])
        root.addWidget(self.splitter, 3)

        bottom = QTabWidget()
        self.alert_log = QTextEdit(readOnly=True)
        self.event_log = QTextEdit(readOnly=True)
        self.note_log = QTextEdit()
        bottom.addTab(self.alert_log, "预警日志")
        bottom.addTab(self.event_log, "异动记录")
        bottom.addTab(self.note_log, "复盘笔记")
        bottom.setMaximumHeight(200)
        root.addWidget(bottom, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage("就绪")

    def _build_topbar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(2, 0, 2, 0)

        self.lbl_market = QLabel("非交易日")
        self.lbl_market.setProperty("role", "muted")
        self.lbl_source = QLabel("数据源：--")
        self.lbl_source.setProperty("role", "warn")
        self.lbl_clock = QLabel("--:--:--")
        self.lbl_clock.setProperty("role", "metric")
        self.lbl_mode = QLabel("单股模式")
        self.lbl_mode.setProperty("role", "info")

        for widget in (self.lbl_market, self.lbl_source, self.lbl_clock):
            layout.addWidget(widget)
            layout.addStretch(1)
        layout.addWidget(self.lbl_mode)
        return bar

    def _build_left(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QLabel("自选分组")
        title.setProperty("role", "muted")
        layout.addWidget(title)
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        for name in DEFAULT_GROUPS:
            tree.addTopLevelItem(QTreeWidgetItem([name]))
        layout.addWidget(tree)
        return panel

    def _build_center(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)
        hint = QLabel("图表区：K线 / 分时 / 多股网格 —— 图表组件按 ui-spec 接入")
        hint.setProperty("role", "muted")
        hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint)
        return panel

    def _build_right(self) -> QWidget:
        """右栏按数据源实际能力渲染：拿不到的面板必须显式说明，不留空壳。"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        caps = self.adapter.capabilities
        panels = (
            ("逐笔成交", caps.tick,
             "该数据源不提供逐笔成交（tqcenter 的 period='tick' 不可用）。\\n"
             "替代：右栏大单监控按快照差分给出分钟级推演，标注「采样推演」。"),
            ("大单监控", True,
             "按快照轮询差分统计区间成交额；非单笔口径，不作为单笔大单结论。"),
            ("概念板块 / 龙虎榜", caps.sector,
             "该数据源无板块接口；龙虎榜需 CSV 导入。"),
        )
        self.right_boxes: dict[str, QTextEdit] = {}
        for title_text, supported, note in panels:
            title = QLabel(title_text if supported else f"{title_text}（不支持）")
            title.setProperty("role", "muted" if supported else "warn")
            layout.addWidget(title)
            box = QTextEdit(readOnly=True)
            color = COLORS["text_muted"] if supported else COLORS["warn"]
            box.setHtml(f"<span style='color:{color}'>{note}</span>")
            box.setMaximumHeight(150)
            layout.addWidget(box)
            self.right_boxes[title_text] = box
        return panel

    # ------------------------------------------------------------ 数据
    def _refresh(self) -> None:
        self.lbl_clock.setText(datetime.now().strftime("%H:%M:%S"))
        caps = self.adapter.capabilities
        self.lbl_source.setText(f"{caps.data_level} 级 · {caps.summary()}")
        try:
            symbols = self.adapter.get_stock_list()[:30]
            quotes = list(self.adapter.get_quotes(symbols).values())
        except DataSourceError as exc:
            self.statusBar().showMessage(f"数据源异常：{exc.message}")
            return
        except Exception as exc:  # 适配器内部已转 DataSourceError，此处兜底防崩
            self.statusBar().showMessage(f"刷新失败：{exc}")
            return

        self.table.setRowCount(len(quotes))
        for row, quote in enumerate(quotes):
            pct = quote.change_pct
            values = [
                quote.name, quote.symbol,
                f"{quote.price:.2f}" if quote.price is not None else "--",
                format_pct(pct), format_amount(quote.amount),
                format_pct(quote.turnover_rate),
                f"{quote.volume_ratio:.2f}" if quote.volume_ratio is not None else "--",
                quote.data_level,
            ]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter if col > 1 else Qt.AlignLeft | Qt.AlignVCenter)
                if col == 3:
                    item.setForeground(QColor(color_for_change(pct)))
                self.table.setItem(row, col, item)
            for event in self.engine.check_price_rules(quote):
                self.alert_log.append(format_message(event))
'''

FILES["app/main.py"] = '''"""桌面端启动入口：先起本地服务线程，再开窗口。"""
from __future__ import annotations

import sys
import threading

from app.config import load_config


def _serve_api(host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError:
        print("未安装 uvicorn/fastapi，本地接口服务不启动（桌面端仍可使用）")
        return
    uvicorn.run("app.api.app:app", host=host, port=port, log_level="warning")


def main() -> int:
    config = load_config()
    port = int(config.get("tdx_bridge", {}).get("http_port", 8611))
    thread = threading.Thread(target=_serve_api, args=("127.0.0.1", port), daemon=True)
    thread.start()

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("缺少 PySide6，请先执行: pip install -r requirements.txt")
        return 1
    from app.ui.main_window import MainWindow

    qt_app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
'''

FILES["scripts/init_db.py"] = '''"""初始化数据库：建表 + 默认分组 + 备份。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, sqlite_path_from_url
from app.db.session import Database


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    config = load_config(root / "config.yaml")
    db_path = sqlite_path_from_url(config["database"]["url"])
    if not db_path.is_absolute():
        db_path = root / db_path
    database = Database(db_path, schema_path=root / "app" / "db" / "schema.sql")
    table_count = database.init_schema()
    database.seed_default_groups()
    print(f"数据库: {db_path}")
    print(f"建表数量: {table_count}")
    print(f"表清单: {', '.join(database.table_names())}")
    print("初始化完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

FILES["scripts/run.py"] = '''"""启动桌面端。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
'''

FILES["scripts/verify_datasource.py"] = '''"""数据源自检：接真实行情并把状态写库。

用法:
    python scripts/verify_datasource.py [symbol]

退出码 0 表示数据源可用；1 表示不可用（错误信息会打印原因，不会伪造数据）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters import build_adapter
from app.adapters.base import DataSourceError
from app.config import load_config, sqlite_path_from_url
from app.db.session import Database


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "600000.SH"
    config = load_config(ROOT / "config.yaml")
    adapter = build_adapter(config)
    caps = adapter.capabilities
    print(f"数据源: {config['datasource']['name']} | 等级 {caps.data_level}")
    print(f"能力: {caps.summary()}")
    print(f"说明: {caps.note}")

    health = adapter.health_check() if hasattr(adapter, "health_check") else {"connected": True}
    print(f"健康: {health}")

    try:
        quote = adapter.get_quote(symbol)
    except DataSourceError as exc:
        print(f"取行情失败 [{exc.kind}] {exc.message}")
        return 1
    pct = f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else "N/A"
    amount = f"{(quote.amount or 0) / 1e8:.2f}亿" if quote.amount else "N/A"
    print(f"行情: {quote.name} {quote.symbol} {quote.price} {pct} {amount} "
          f"换手{quote.turnover_rate} 量比{quote.volume_ratio} 时间源={quote.time_source}")

    try:
        bars = adapter.get_kline(symbol, "1d", limit=5)
        print(f"日K: {len(bars)} 根, 最后 {bars[-1].bar_time:%Y-%m-%d} close={bars[-1].close}")
    except DataSourceError as exc:
        print(f"日K不可用 [{exc.kind}] {exc.message}")

    for period in ("5m", "1m"):
        try:
            adapter.get_kline(symbol, period, limit=5)
            print(f"{period}: 意外可用，请复核能力矩阵")
        except DataSourceError as exc:
            print(f"{period}: 按预期不可用 [{exc.kind}]")

    # 落库：行情缓存 + 数据源状态，证明链路通到 SQLite
    db_path = sqlite_path_from_url(config["database"]["url"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    database = Database(db_path, schema_path=ROOT / "app" / "db" / "schema.sql")
    if not db_path.exists():
        database.init_schema()
    now = datetime.now().isoformat(timespec="seconds")
    with database.cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO stock_info (symbol, code, name, market, source) VALUES (?, ?, ?, ?, ?)",
            (quote.symbol, quote.symbol.split(".")[0], quote.name,
             quote.symbol.split(".")[1], quote.source),
        )
        cur.execute(
            """INSERT INTO quotes_cache
               (symbol, price, prev_close, open, high, low, volume, amount,
                change_pct, turnover_rate, volume_ratio, limit_up_price, limit_down_price,
                data_level, source, quote_time, received_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 price=excluded.price, amount=excluded.amount,
                 data_level=excluded.data_level, quote_time=excluded.quote_time,
                 received_at=excluded.received_at, updated_at=excluded.updated_at""",
            (quote.symbol, quote.price, quote.prev_close, quote.open, quote.high, quote.low,
             quote.volume, quote.amount, quote.change_pct, quote.turnover_rate, quote.volume_ratio,
             quote.limit_up_price, quote.limit_down_price, quote.data_level, quote.source,
             quote.quote_time.isoformat(timespec="seconds") if quote.quote_time else None,
             quote.received_at.isoformat(timespec="seconds") if quote.received_at else None,
             now),
        )
        cur.execute(
            """INSERT INTO data_source_status (source, data_level, connected, latency_ms, data_timestamp, checked_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(source, data_level) DO UPDATE SET
                 connected=1, latency_ms=excluded.latency_ms,
                 data_timestamp=excluded.data_timestamp, checked_at=excluded.checked_at""",
            (config["datasource"]["name"], caps.data_level,
             health.get("latency_ms"), now, now),
        )
    with database.cursor() as cur:
        cur.execute("SELECT symbol, price, change_pct, data_level, updated_at FROM quotes_cache")
        rows = cur.fetchall()
    print(f"落库: quotes_cache {len(rows)} 行 -> {[dict(r) for r in rows]}")

    if hasattr(adapter, "disconnect"):
        adapter.disconnect()
    print("数据源自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

FILES["tests/test_smoke.py"] = '''"""骨架冒烟测试：规范化、分档、建库、去重。"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.large_order import classify_tier, tag_direction
from app.services.tdx_bridge import ingest, normalize_symbol


def test_normalize_symbol():
    assert normalize_symbol("600000") == "600000.SH"
    assert normalize_symbol("000001") == "000001.SZ"
    assert normalize_symbol("300001") == "300001.SZ"
    assert normalize_symbol("688001") == "688001.SH"
    assert normalize_symbol("600000.SH") == "600000.SH"
    assert normalize_symbol("300001.SZ") == "300001.SZ"
    assert normalize_symbol("300001.SH") is None      # 前缀与后缀冲突
    assert normalize_symbol("abc") is None
    assert normalize_symbol("60000") is None


def test_classify_tier():
    assert classify_tier(500_000) is None
    assert classify_tier(1_500_000) == "normal"
    assert classify_tier(3_500_000) == "large"
    assert classify_tier(6_000_000) == "super"
    assert classify_tier(12_000_000) == "continuous"


def test_tag_direction():
    assert tag_direction(900_000, 100_000) == "买入活跃"
    assert tag_direction(100_000, 900_000) == "卖出活跃"
    assert tag_direction(500_000, 500_000) == "买卖分歧"
    assert tag_direction(0, 0) == "资金行为不明确"


def test_database_init_and_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
        db = Database(Path(tmp) / "test.db", schema_path=schema)
        first = db.init_schema()
        second = db.init_schema()
        assert first == second
        db.seed_default_groups()
        assert "watch_groups" in db.table_names()
        assert "alert_events" in db.table_names()


def test_tdx_ingest_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        schema = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"
        db = Database(Path(tmp) / "test.db", schema_path=schema)
        db.init_schema()
        payload = {
            "source": "tongdaxin", "event": "watchlist_update",
            "timestamp": "2026-11-08T10:30:00",
            "symbols": [{"code": "600000", "name": "示例", "group": "龙头观察"}],
        }
        with db.cursor() as cur:
            result = ingest(cur, payload, channel="test")
        assert result == {"total": 1, "new": 1}
        with db.cursor() as cur:
            again = ingest(cur, payload, channel="test")
        assert again == {"total": 1, "new": 0}


def test_alert_cooldown():
    engine = AlertEngine(cooldown_sec=300)
    now = datetime(2026, 11, 8, 10, 0, 0)
    assert engine.should_fire("600000.SH", "r1", "near_limit_up", 11.0, now) is True
    assert engine.should_fire("600000.SH", "r1", "near_limit_up", 11.0, now) is False


# ---------------------------------------------------------------- tqcenter
def test_tq_symbol_precheck():
    """坏代码会触发 tqcenter 内部 tq.close()，必须先本地拦下。"""
    from app.adapters.tqcenter_adapter import _valid_symbol
    assert _valid_symbol("600000.SH")
    assert _valid_symbol("300001.SZ")
    assert not _valid_symbol("600000")
    assert not _valid_symbol("300001.SH")
    assert not _valid_symbol("")


def test_tq_minute_kline_declared_unsupported():
    from app.adapters.base import DataSourceError
    from app.adapters.tqcenter_adapter import TqCenterAdapter
    adapter = TqCenterAdapter()
    caps = adapter.capabilities
    assert caps.tick is False
    assert caps.minute_kline is False
    assert caps.order_book_levels == 5
    assert caps.subscribe_max == 100
    try:
        adapter.get_kline("600000.SH", "5m")
    except DataSourceError as exc:
        assert exc.kind == "unsupported"
    else:
        raise AssertionError("分钟线必须报 unsupported，不得用日线顶替")


def test_snapshot_sampler_delta():
    """快照差分：手->股、万元->元，方向按内外盘增量推演。"""
    from app.adapters.tqcenter_adapter import SnapshotSampler
    sampler = SnapshotSampler()
    t0 = datetime(2026, 11, 6, 10, 0, 0)
    first = sampler.feed("600000.SH", {
        "Now": "10.00", "Volume": "1000", "Amount": "1000.00",
        "Inside": "400", "Outside": "600", "Average": "10.00",
    }, t0)
    assert first is None, "首帧没有差分基线"
    point = sampler.feed("600000.SH", {
        "Now": "10.10", "Volume": "1300", "Amount": "1030.30",
        "Inside": "500", "Outside": "800", "Average": "10.05",
    }, t0 + timedelta(seconds=3))
    assert point.interval_volume == 30000          # 300 手 * 100
    assert round(point.interval_amount) == 303000  # 30.3 万元 -> 元
    assert point.direction == "buy"                # 外盘增量 200 > 内盘 100
    assert point.quality_flag == "sampled"
'''

FILES["README.md"] = '''# 寻龙盯盘 Dragon Hunter

A股短线交易工作台。盘前选股 → 盘中盯盘 → 资金异动 → 龙头对比 → 预警提醒 → 收盘复盘 → 本地记忆。

> 本工具为行情分析与决策辅助工具，**不代下单**，不构成投资建议。

## 快速开始

```bash
python -m venv .venv
.venv\\Scripts\\activate            # Windows
pip install -r requirements.txt
cp config.example.yaml config.yaml  # 已默认指向 tqcenter
python scripts/init_db.py
python -m app.adapters.tqcenter_adapter 600000.SH   # 先验数据源
python scripts/run.py               # 启动桌面端
```

本地服务默认 `http://127.0.0.1:8611`，`GET /health` 返回数据源能力矩阵。
桌面端依赖 PySide6；只想跑本地服务：`uvicorn app.api.app:app --port 8611`。

## 数据源：tqcenter（通达信 TdxQuant）

默认配置：

```yaml
datasource:
  name: tqcenter
  user_dir: "D:/new_tdx64/PYPlugins/user"
  poll_interval_sec: 3
```

依赖同级的 `PYPlugins/TPythClient.dll`；无需通达信窗口在前台，但客户端必须安装且本地数据完整。

| 能力 | 状态 | 说明 |
|---|---|---|
| 日/周/月 K | 可用 | Amount 单位为万元，适配器已换算为元 |
| 实时快照 + 五档 | 可用 | ~11ms/只，Volume/内外盘单位为手 |
| 行业 | 可用 | 需显式请求 `rs_hyname` |
| 板块 / 成分股 | 部分 | 个别板块成分异常，缺失时界面标灰 |
| 分钟 K | **不可用** | 1m/5m/15m/30m/60m 返回空 |
| 逐笔成交 | **不可用** | `period='tick'` 返回 error=-5 |
| 龙虎榜 | **不可用** | 改 CSV 导入 |

因此：分时用 `SnapshotSampler` 采样（标「采样分时」，`quality_flag='sampled'`），
大单按区间累计成交额判定（标「分钟内累计」），逐笔面板显示不支持而不是空表。

实测性能：单只全字段 ~31ms，12 只 115~373ms，股票池 5224 只 1.1s。
100 只以上请分组轮询（可见分组优先），tqcenter 订阅上限只有 100 只且回调不含行情字段。

## 目录

```
app/adapters/    base.py(协议) / mock.py(D级) / tqcenter_adapter.py
app/services/    大单识别 / 预警引擎 / 通达信桥接
app/db/          SQLite 访问与 schema.sql
app/api/         FastAPI 本地接口 + WebSocket
app/ui/          主题、主窗口、图表组件
scripts/         init_db.py / run.py
tests/           冒烟测试
```

## 已完成 / 未完成

已完成：工程骨架、SQLite 17 张业务表建库、默认 10 个分组、tqcenter 适配器（真实数据校验通过）、
代码预校验、快照采样引擎、大单分档与方向标签、价格预警冷却、通达信 HTTP/CSV 解析、暗色主题、主窗口五区布局、能力矩阵驱动的不支持面板提示。

未完成（后续阶段）：图表渲染组件、采样分时落库与回放、多股网格、板块强弱排名、龙虎榜导入、
JSON 文件监听通道、AI 复盘报告、断线重连与缺口补齐。

## 自检

```bash
python -m pytest -q
python scripts/init_db.py
python -m app.adapters.tqcenter_adapter 600000.SH
```

## 铁律

红涨绿跌；数据必带来源与等级；缺失标 `--` 不编造；没有的能力显式说不支持；大单只描述成交不推断身份；
预警只提示不给买卖指令；分组与布局必须落库并重启恢复；调用数据源前先本地校验代码。
'''

FILES["config.example.yaml_NOTE"] = ""  # 占位，不写入


def write_file(root: Path, rel: str, content: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def build(root: Path, force: bool = False) -> list[str]:
    created: list[str] = []
    for rel, content in FILES.items():
        if rel.endswith("_NOTE"):
            continue
        write_file(root, rel, content)
        created.append(rel)

    # 单一 DDL 来源
    if not SCHEMA_SRC.exists():
        raise FileNotFoundError(f"缺少 schema 源文件: {SCHEMA_SRC}")
    (root / "app" / "db").mkdir(parents=True, exist_ok=True)
    shutil.copy2(SCHEMA_SRC, root / "app" / "db" / "schema.sql")
    created.append("app/db/schema.sql")

    shutil.copy2(THEME_SRC, root / "app" / "ui" / "theme.css")
    created.append("app/ui/theme.css")

    if ADAPTER_SRC.exists():
        shutil.copy2(ADAPTER_SRC, root / "app" / "adapters" / "tqcenter_adapter.py")
        created.append("app/adapters/tqcenter_adapter.py")

    if REQ_SRC.exists():
        shutil.copy2(REQ_SRC, root / "requirements.txt")
        created.append("requirements.txt")
    if CONFIG_SRC.exists():
        shutil.copy2(CONFIG_SRC, root / "config.example.yaml")
        created.append("config.example.yaml")
        config_path = root / "config.yaml"
        if not config_path.exists() or force:
            shutil.copy2(CONFIG_SRC, config_path)
            created.append("config.yaml")

    (root / "data").mkdir(exist_ok=True)
    (root / "data" / ".gitkeep").write_text("", encoding="utf-8")

    # 建库并写入默认分组
    sys.path.insert(0, str(root))
    from app.db.session import Database  # noqa: E402

    database = Database(root / "data" / "dragon_hunter.db", schema_path=root / "app" / "db" / "schema.sql")
    table_count = database.init_schema()
    database.seed_default_groups()
    created.append(f"data/dragon_hunter.db（{table_count} 张表）")
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description="生成寻龙盯盘工程骨架")
    parser.add_argument("target", help="目标目录")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 config.yaml 等文件")
    args = parser.parse_args()

    root = Path(args.target).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    existing = [p for p in root.iterdir() if p.name not in ("data",)]
    if existing and not args.force:
        print(f"目标目录非空: {root}")
        print("请使用空目录，或加 --force 覆盖同名文件")
        return 1

    created = build(root, force=args.force)
    print(f"骨架已生成: {root}")
    for item in created:
        print(f"  + {item}")
    print("\n下一步:")
    print(f"  cd {root}")
    print("  pip install -r requirements.txt")
    print("  python scripts/init_db.py")
    print("  python scripts/run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
