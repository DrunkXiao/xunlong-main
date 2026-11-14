"""通达信本地日线数据源（只读 vipdoc/*/lday/*.day，不联网、不连客户端）。

用途：全市场日线扫描（强势回调选股、涨停四阶选股）。与 tqcenter 的分工：

- tqcenter 是逐只快照/单只K线，实时性好但全市场拉一遍要 130 秒，还会占用客户端会话；
- 本模块直接读 .day 文件，4400 只约 60 秒，离线可用，也不影响盘中轮询。

目录结构（以 D:/new_tdx64 为例）：

    vipdoc/sh/lday/sh600000.day     沪市日线
    vipdoc/sz/lday/sz000001.day     深市日线
    vipdoc/sh/lday/sh880305.day     通达信 880xxx 行业指数日线（板块强度用）
    vipdoc/sh/lday/sh000001.day     上证指数（交易日历与指数快照用）
    T0002/hq_cache/shs.tnf          证券名称（复用 tdx_names 的解析）
    T0002/hq_cache/base.dbf         股本（流通 A 股 / 上市日），dBase III
    T0002/hq_cache/tdxhy.cfg        个股 -> 通达信行业代码
    T0002/hq_cache/tdxzs.cfg        行业代码 -> 行业名称 / 880xxx 指数代码

日线文件格式：每条 32 字节，无文件头。

    u32 date / open / high / low / close（价格单位为分）
    f32 amount（元）
    u32 volume（股）
    u32 reserved

口径（与 tqcenter 快照不同，混用会放大 100 倍）：

| 字段 | 本模块 | tqcenter 快照 |
|---|---|---|
| 成交量 | 股 | 手 |
| 成交额 | 元 | 万元 |

价格为**不复权**：除权除息日前后会有跳空缺口，不能当成有效回调或突破，
扫描结果里只按技术形态判定，除权日前后请人工核对（README「已知限制」有写）。
"""
from __future__ import annotations

import struct
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import numpy as np
import pandas as pd

from app.adapters.tdx_names import load_name_index, resolve_cache_dir

RECORD_SIZE = 32

DAY_DTYPE = np.dtype([
    ("date", "<u4"),
    ("open", "<u4"),
    ("high", "<u4"),
    ("low", "<u4"),
    ("close", "<u4"),
    ("amount", "<f4"),
    ("volume", "<u4"),
    ("reserved", "<u4"),
])

# 只保留 A 股主板与创业板；指数、基金、债券、可转债都在同一目录里
DEFAULT_PREFIXES = ("60", "00", "30")

# 各市场的 A 股代码段：沪市 000xxx 是指数，深市 399xxx 才是深证指数
MARKET_PREFIXES = {"SH": ("60", "68"), "SZ": ("00", "30"), "BJ": ("43", "83", "87", "88")}

_MARKET_FLAG = {"0": "SZ", "1": "SH", "2": "BJ"}

# 涨停四阶段选股的股票范围：沪深主板 + 创业板。科创板/北交所/B 股一律不纳入。
# 按**两位**前缀判断：交易所会新增代码段（302 就是创业板新段，如中航成飞 302132），
# 枚举三位段必然漏——漏掉的后果不只是少选一只，而是把 20% 涨跌幅当成 10%，涨停价直接算错。
MAIN_SH = ("60",)
MAIN_SZ = ("00",)
GEM = ("30",)
STAR = ("68",)
BSE = ("4", "8", "920")


def board_of(code: str) -> str:
    """板块归属（通达信口径）。"""
    if code.startswith(MAIN_SH):
        return "沪主板"
    if code.startswith(MAIN_SZ):
        return "深主板"
    if code.startswith(GEM):
        return "创业板"
    if code.startswith(STAR):
        return "科创板"
    if code.startswith(BSE):
        return "北交所"
    return "其他"


def in_limitup_scope(code: str) -> bool:
    """是否属于涨停四阶段选股范围。"""
    return board_of(code) in ("沪主板", "深主板", "创业板")


def limit_rate_of(code: str, name: str = "") -> float:
    """涨跌幅限制（小数）。ST 主板 5%、创业板/科创板 20%、主板 10%。"""
    board = board_of(code)
    is_st = ("ST" in (name or "").upper())
    if board in ("创业板", "科创板"):
        return 0.20
    if board == "北交所":
        return 0.30
    return 0.05 if is_st else 0.10


def round_half_up(value: float, digits: int = 2) -> float:
    """A 股价格规则是四舍五入到分，不能直接用 round()（银行家舍入）。"""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def limit_prices(prev_close: float, rate: float) -> tuple[float, float]:
    """(涨停价, 跌停价)，按四舍五入到分。"""
    if prev_close <= 0:
        return 0.0, 0.0
    return (round_half_up(prev_close * (1 + rate)),
            round_half_up(prev_close * (1 - rate)))


def asof(bars: list, date: int) -> list:
    """截断到目标交易日（含）。历史回放必须走这里，杜绝未来数据。"""
    if not bars:
        return []
    if bars[-1]["date"] <= date:
        return bars
    return [b for b in bars if b["date"] <= date]


class TdxDailyError(RuntimeError):
    """本地日线目录不可用（没装通达信、目录指错、日线未下载）。"""


def is_st_name(name: str) -> bool:
    """名称含 ST / *ST / 退 即视为风险警示或退市整理，扫描时剔除。"""
    if not name:
        return False
    return "ST" in name.upper() or "退" in name


def limit_up_pct(code: str, is_st: bool = False) -> float:
    """按板块返回涨停幅度（百分比）。"""
    if code.startswith(("30", "68")):        # 创业板 / 科创板
        return 20.0
    return 5.0 if is_st else 10.0            # 主板，ST 为 5%


def limit_up_cents(prev_close_cents: int, pct: float) -> int:
    """以分为单位算涨停价，四舍五入到 0.01（A 股规则，不用涨跌幅近似）。"""
    bp = int(round(pct * 100))               # 10.0 -> 1000
    return (int(prev_close_cents) * (10000 + bp) + 5000) // 10000


def market_closed(now: datetime | None = None) -> bool:
    """当日 K 线是否已定稿。15:05 之前（含午休、盘中）都视为「未收盘」。

    收盘集合竞价在 15:00 结束，留 5 分钟给客户端落盘：宁可多等 5 分钟，
    也不要用一根还在变的 K 线去确认阳包阴/锤子线。
    """
    moment = now or datetime.now()
    return (moment.hour, moment.minute) >= (15, 5)


def trim_unclosed(bars: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """盘中去掉当日未收盘的那一根，避免用半根 K 线出信号。"""
    if bars is None or bars.empty or market_closed(now):
        return bars
    today = (now or datetime.now()).date()
    if bars["date"].iloc[-1].date() == today:
        return bars.iloc[:-1].reset_index(drop=True)
    return bars


def resolve_root(config: dict) -> Path | None:
    """定位通达信安装目录：配置显式指定 > 由 tqcenter 的 user 目录上溯 > 常见默认路径。

    上溯而不是写死 D:/new_tdx64：用户换安装盘时只改 `datasource.user_dir` 一处即可。
    """
    pb = (config or {}).get("pullback") or {}
    lb = (config or {}).get("limitup") or {}
    explicit = str(pb.get("tdx_root") or lb.get("tdx_root") or "").strip()
    if explicit:
        path = Path(explicit)
        if (path / "vipdoc").is_dir():
            return path
    user_dir = str(((config or {}).get("datasource") or {}).get("user_dir") or "").strip()
    if user_dir:
        for base in (Path(user_dir), *Path(user_dir).parents):
            if (base / "vipdoc").is_dir():
                return base
    for guess in (Path("D:/new_tdx64"), Path("C:/new_tdx64")):
        if (guess / "vipdoc").is_dir():
            return guess
    return None


class TdxDailySource:
    """本地日线：股票池、日线、名称、行业。实例内部缓存名称与行业表。"""

    def __init__(self, root, markets=("SH", "SZ"), tail_bars: int = 160,
                 min_bars: int = 60, prefixes=DEFAULT_PREFIXES,
                 exclude_st: bool = True) -> None:
        self.root = Path(root)
        self.vipdoc = self.root / "vipdoc"
        if not self.vipdoc.is_dir():
            raise TdxDailyError(
                f"未找到通达信行情目录：{self.vipdoc}。"
                "请检查 config.yaml 的 pullback.tdx_root 或 datasource.user_dir 是否指向通达信安装目录。"
            )
        self.markets = tuple(str(m).upper() for m in markets)
        self.tail_bars = int(tail_bars)
        self.min_bars = int(min_bars)
        self.prefixes = tuple(str(p) for p in (prefixes or DEFAULT_PREFIXES))
        self.exclude_st = bool(exclude_st)
        self._dirs = {m: self.vipdoc / m.lower() / "lday" for m in self.markets}
        self._names: dict[str, str] | None = None
        self._industries: dict[str, str] | None = None
        self._caps: dict[str, dict] | None = None
        self._sectors: dict[str, dict] | None = None
        self._calendar: list[int] | None = None

    # ---------------------------------------------------------------- 构造
    @classmethod
    def from_config(cls, config: dict) -> "TdxDailySource":
        pb = (config or {}).get("pullback") or {}
        root = resolve_root(config)
        if root is None:
            raise TdxDailyError(
                "找不到通达信安装目录：vipdoc 不存在。"
                "请在 config.yaml 里设置 pullback.tdx_root（例如 D:/new_tdx64）。"
            )
        return cls(
            root,
            markets=pb.get("markets") or ["SH", "SZ"],
            tail_bars=int(pb.get("tail_bars", 160)),
            min_bars=int(pb.get("min_bars", 60)),
            prefixes=pb.get("universe_prefixes") or DEFAULT_PREFIXES,
            exclude_st=bool(pb.get("exclude_st", True)),
        )

    # ---------------------------------------------------------------- 元数据
    def names(self) -> dict[str, str]:
        """symbol('600000.SH') -> 名称。缺文件时返回空表，调用方回落代码。"""
        if self._names is None:
            cache = resolve_cache_dir(self.root) or (self.root / "T0002" / "hq_cache")
            self._names = load_name_index(cache)
        return self._names

    def industries(self) -> dict[str, str]:
        """symbol -> 通达信行业名称。tdxhy/tdxzs 任一缺失只影响行业列，不影响扫描。"""
        if self._industries is None:
            cache = resolve_cache_dir(self.root) or (self.root / "T0002" / "hq_cache")
            tnames: dict[str, str] = {}
            zs = cache / "tdxzs.cfg"
            if zs.exists():
                for line in zs.read_text(encoding="gbk", errors="replace").splitlines():
                    field = line.split("|")
                    if len(field) >= 6 and field[5].startswith("T"):
                        tnames[field[5]] = field[0]
            out: dict[str, str] = {}
            hy = cache / "tdxhy.cfg"
            if hy.exists():
                for line in hy.read_text(encoding="gbk", errors="replace").splitlines():
                    field = line.split("|")
                    if len(field) < 3 or not field[1].strip():
                        continue
                    market = _MARKET_FLAG.get(field[0].strip())
                    if not market:
                        continue
                    code = field[1].strip()
                    industry = tnames.get(field[2].strip(), field[2].strip())
                    out[f"{code}.{market}"] = industry or "未分类"
            self._industries = out
        return self._industries

    # ---------------------------------------------------------------- 股本 / 行业
    def caps(self) -> dict[str, dict]:
        """T0002/hq_cache/base.dbf（dBase III）：流通 A 股、总股本（万股）、上市日、行业号。

        换手率 = 成交量(股) / 流通股本(股)，缺股本时换手率只能置空，
        绝不能用总股本或流通市值反推——那样算出来的换手是假的。
        """
        if self._caps is not None:
            return self._caps
        path = self._hq_dir() / "base.dbf"
        out: dict[str, dict] = {}
        if not path.exists():
            self._caps = out
            return out
        raw = path.read_bytes()
        if len(raw) < 32:
            self._caps = out
            return out
        total, header, row_len = struct.unpack("<IHH", raw[4:12])
        fields: dict[str, tuple[int, int]] = {}
        offset = 32
        while offset + 32 <= len(raw) and raw[offset] != 0x0D:
            block = raw[offset:offset + 32]
            name = block[:11].split(b"\x00")[0].decode("ascii", errors="ignore")
            fields[name] = (block[16], struct.unpack("<I", block[12:16])[0])
            offset += 32
        if not {"GPDM", "LTAG", "ZGB"} <= fields.keys():
            self._caps = out
            return out

        def text(rec: bytes, key: str) -> str:
            length, start = fields[key]
            return rec[start:start + length].decode("gbk", errors="ignore").strip()

        def number(rec: bytes, key: str) -> float:
            try:
                return float(text(rec, key) or 0)
            except ValueError:
                return 0.0

        for index in range(total):
            start = header + index * row_len
            rec = raw[start:start + row_len]
            if len(rec) < row_len:
                break
            code = text(rec, "GPDM")
            if len(code) != 6 or not code.isdigit():
                continue
            out[code] = {
                "float_share": number(rec, "LTAG"),     # 万股
                "total_share": number(rec, "ZGB"),      # 万股
                "list_date": text(rec, "SSDATE"),
                "industry_no": text(rec, "HY"),
                "cap_date": text(rec, "GXRQ"),
            }
        self._caps = out
        return out

    def sectors(self) -> dict[str, dict]:
        """{'by_tcode': {T代码: {name, index_code}}, 'by_stock': {symbol: T代码}}。"""
        if self._sectors is not None:
            return self._sectors
        cache = self._hq_dir()
        by_tcode: dict[str, dict] = {}
        by_stock: dict[str, str] = {}
        zs = cache / "tdxzs.cfg"
        if zs.exists():
            for line in zs.read_text(encoding="gbk", errors="replace").splitlines():
                field = line.split("|")
                if len(field) < 6 or not field[5].startswith("T"):
                    continue
                by_tcode.setdefault(field[5].strip(), {
                    "name": field[0].strip(),
                    "index_code": field[1].strip(),
                })
        hy = cache / "tdxhy.cfg"
        if hy.exists():
            for line in hy.read_text(encoding="gbk", errors="replace").splitlines():
                field = line.split("|")
                if len(field) < 3 or not field[1].strip().isdigit():
                    continue
                market = _MARKET_FLAG.get(field[0].strip())
                if not market:
                    continue
                by_stock[f"{field[1].strip()}.{market}"] = field[2].strip()
        self._sectors = {"by_tcode": by_tcode, "by_stock": by_stock}
        return self._sectors

    def sector_of(self, symbol: str) -> dict:
        data = self.sectors()
        tcode = data["by_stock"].get(symbol, "")
        info = data["by_tcode"].get(tcode, {})
        return {
            "tcode": tcode,
            "name": info.get("name") or (tcode or "未分类"),
            "index_code": info.get("index_code", ""),
        }

    def sector_strength(self, date: int) -> dict[str, dict]:
        """用 880xxx 行业指数算板块当日/5 日强度。缺该日数据时返回空，由调用方降级聚合。"""
        out: dict[str, dict] = {}
        data = self.sectors()
        seen: set[str] = set()
        for tcode, info in data["by_tcode"].items():
            index_code = info.get("index_code") or ""
            if not index_code or index_code in seen:
                continue
            seen.add(index_code)
            path = self._dirs.get("SH") or (self.vipdoc / "sh" / "lday")
            file = path / f"sh{index_code}.day"
            if not file.exists():
                continue
            bars = self._read(file, 6)
            if not bars or bars[-1]["date"] != date:
                continue
            last = bars[-1]
            change_5d = None
            if len(bars) >= 6 and bars[-6]["close"] > 0:
                change_5d = round((last["close"] / bars[-6]["close"] - 1) * 100, 2)
            out[tcode] = {
                "name": info["name"],
                "index_code": index_code,
                "sector_change": round(last["pct"], 2),
                "sector_change_5d": change_5d,
                "sector_amount": last["amount"],
                "caliber": "通达信 880xxx 行业指数",
            }
        return out

    def index_snapshot(self, date: int) -> tuple[dict, dict]:
        """上证/深证/创业板指收盘与涨跌幅 + 沪深两市成交额（亿元）。"""
        indices: dict[str, dict] = {}
        amounts = {"sh_yi": 0.0, "sz_yi": 0.0}
        for name, market, code in (("上证指数", "SH", "000001"),
                                   ("深证成指", "SZ", "399001"),
                                   ("创业板指", "SZ", "399006")):
            path = self._dirs.get(market, self.vipdoc / market.lower() / "lday") / \
                f"{market.lower()}{code}.day"
            if not path.exists():
                continue
            bars = self._read(path, 3)
            if not bars or bars[-1]["date"] != date:
                continue
            last = bars[-1]
            indices[name] = {"close": last["close"], "pct": round(last["pct"], 2)}
            if name == "上证指数":
                amounts["sh_yi"] = round(last["amount"] / 1e8, 0)
            elif name == "深证成指":
                amounts["sz_yi"] = round(last["amount"] / 1e8, 0)
        amounts["total_yi"] = amounts["sh_yi"] + amounts["sz_yi"]
        return indices, amounts

    def calendar(self, count: int = 400) -> list[int]:
        """交易日历：以上证指数日线的日期为准（个股停牌不影响日历）。"""
        if self._calendar is None:
            path = (self._dirs.get("SH") or (self.vipdoc / "sh" / "lday")) / "sh000001.day"
            bars = self._read(path, count) if path.exists() else []
            self._calendar = [b["date"] for b in bars]
        return self._calendar

    def _hq_dir(self) -> Path:
        return resolve_cache_dir(self.root) or (self.root / "T0002" / "hq_cache")

    @staticmethod
    def _read(path: Path, count: int) -> list[dict]:
        """读取最近 count 根日线（尾读），返回 [date(int), open/high/low/close, amount, volume, pct]。"""
        path = Path(path)
        if not path.exists():
            return []
        size = path.stat().st_size
        take = min(count, size // RECORD_SIZE)
        if take <= 0:
            return []
        with path.open("rb") as fh:
            fh.seek(size - take * RECORD_SIZE)
            raw = fh.read(take * RECORD_SIZE)
        arr = np.frombuffer(raw, dtype=DAY_DTYPE)
        dates = arr["date"].astype("int64")
        opens = arr["open"].astype("float64") / 100.0
        highs = arr["high"].astype("float64") / 100.0
        lows = arr["low"].astype("float64") / 100.0
        closes = arr["close"].astype("float64") / 100.0
        amounts = arr["amount"].astype("float64")
        volumes = arr["volume"].astype("float64")
        out: list[dict] = []
        prev = None
        for i in range(take):
            close = float(closes[i])
            pct = (close / prev - 1) * 100 if prev else 0.0
            out.append({
                "date": int(dates[i]),
                "open": float(opens[i]),
                "high": float(highs[i]),
                "low": float(lows[i]),
                "close": close,
                "amount": float(amounts[i]),
                "volume": float(volumes[i]),
                "pct": pct,
                "pre_close": prev if prev else float(opens[i]),
            })
            prev = close
        return out

    def bars_list(self, row: dict, count: int | None = None) -> list[dict]:
        """单只日线（list[dict]），给规则引擎用；比 DataFrame 快一个量级。"""
        path = row.get("path")
        if path is None:
            path = (self._dirs.get(row["market"]) or self.vipdoc / row["market"].lower() / "lday") / \
                f"{row['market'].lower()}{row['code']}.day"
        return self._read(Path(path), int(count or self.tail_bars))

    def last_trade_date(self) -> int | None:
        """最新**已收盘**交易日 YYYYMMDD，优先取指数日线（个股可能停牌）。

        盘中指数日线里已经有当天那根未完成的 K 线，这里直接跳过，
        让「数据日期」始终指向最后一份定稿的日线。
        """
        skip = None if market_closed() else int(datetime.now().strftime("%Y%m%d"))
        best: int | None = None
        for market, code in (("SH", "000001"), ("SZ", "399001"), ("SZ", "399006")):
            path = self.vipdoc / market.lower() / "lday" / f"{market.lower()}{code}.day"
            if not path.exists() or path.stat().st_size < RECORD_SIZE:
                continue
            with path.open("rb") as fh:
                fh.seek(-min(path.stat().st_size, RECORD_SIZE * 8), 2)
                dates = [int(v) for v in np.frombuffer(fh.read(), dtype="<u4")[::8]]
            for day in reversed([d for d in dates if d != skip]):
                if best is None or day > best:
                    best = day
                break
        return best

    # ---------------------------------------------------------------- 股票池
    def universe(self, prefixes=None, exclude_st: bool | None = None,
                 limit: int = 0, date: int | None = None,
                 min_list_days: int = 0, require_caps: bool = False) -> list[dict]:
        """扫描 lday 目录，返回可直接用于评估的股票池（已剔除 ST 与新股）。

        date / min_list_days：按 base.dbf 的上市日期剔除次新（上市不足 N 个自然日）。
        require_caps：缺股本的直接跳过（涨停四阶要算换手率，宁可少一只也不给假换手）。
        """
        names = self.names()
        industries = self.industries()
        caps = self.caps()
        prefixes = tuple(str(p) for p in (prefixes or self.prefixes))
        if exclude_st is None:
            exclude_st = self.exclude_st
        rows: list[dict] = []
        for market, directory in self._dirs.items():
            if not directory.is_dir():
                continue
            market_prefixes = MARKET_PREFIXES.get(market, ())
            allowed = tuple(
                p for p in prefixes
                if any(p.startswith(mp) or mp.startswith(p) for mp in market_prefixes)
            )
            if not allowed:
                continue
            for path in directory.glob(f"{market.lower()}*.day"):
                code = path.stem[2:]
                if len(code) != 6 or not code.isdigit() or not code.startswith(allowed):
                    continue
                bars = path.stat().st_size // RECORD_SIZE
                if bars < self.min_bars:
                    continue
                symbol = f"{code}.{market}"
                name = names.get(symbol, "")
                if exclude_st and is_st_name(name):
                    continue
                cap = caps.get(code) or {}
                if require_caps and not (cap.get("float_share") or 0) > 0:
                    continue
                if date and min_list_days > 0:
                    listed = str(cap.get("list_date") or "")
                    if len(listed) == 8:
                        try:
                            gap = (datetime.strptime(str(date), "%Y%m%d")
                                   - datetime.strptime(listed, "%Y%m%d")).days
                            if gap < min_list_days:
                                continue
                        except ValueError:
                            pass
                rows.append({
                    "symbol": symbol,
                    "code": code,
                    "market": market,
                    "name": name or code,
                    "industry": industries.get(symbol, "未分类"),
                    "board": board_of(code),
                    "limit_rate": limit_rate_of(code, name),
                    "float_share": float(cap.get("float_share") or 0.0),
                    "list_date": str(cap.get("list_date") or ""),
                    "sector": self.sector_of(symbol),
                    "bars": bars,
                    "path": path,
                })
        rows.sort(key=lambda r: (r["market"], r["code"]))
        return rows[:limit] if limit else rows

    # ---------------------------------------------------------------- 日线
    def bars(self, row: dict) -> pd.DataFrame:
        """读单只日线，只解析最近 tail_bars 根（够算 MA20、新高与缩量窗口）。"""
        path = row.get("path")
        if path is None:
            path = self.vipdoc / row["market"].lower() / "lday" / \
                f"{row['market'].lower()}{row['code']}.day"
        raw = Path(path).read_bytes()
        total = len(raw) // RECORD_SIZE
        if total < 2:
            raise TdxDailyError(f"{row.get('symbol') or path} 日线不足 2 根")
        take = min(total, self.tail_bars)
        start = (total - take) * RECORD_SIZE
        arr = np.frombuffer(raw[start:start + take * RECORD_SIZE], dtype=DAY_DTYPE)
        frame = pd.DataFrame({
            "date": pd.to_datetime(arr["date"].astype("int64").astype(str), format="%Y%m%d"),
            "open": arr["open"].astype("float64") / 100.0,
            "high": arr["high"].astype("float64") / 100.0,
            "low": arr["low"].astype("float64") / 100.0,
            "close": arr["close"].astype("float64") / 100.0,
            "amount": arr["amount"].astype("float64"),
            "volume": arr["volume"].astype("float64"),
            # 整数分价：涨停价必须按分比较，浮点会把 10.00 算成 9.999999
            "open_cents": arr["open"].astype("int64"),
            "high_cents": arr["high"].astype("int64"),
            "close_cents": arr["close"].astype("int64"),
        })
        frame = frame.sort_values("date").drop_duplicates("date").reset_index(drop=True)
        frame["pct_change"] = frame["close"].pct_change() * 100.0
        return frame
