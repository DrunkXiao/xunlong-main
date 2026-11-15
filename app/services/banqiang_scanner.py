"""量学板枪扫描：读本地日线 → 五阶段识别 → 评分 → 状态分组 → 落盘。

跑在独立线程里（`BanqiangScanner`），不排进 `BackgroundJobs` 的串行队列：
一次全市场扫描十几到几十秒，排在切股任务前面会把界面堵住。

数据闸门（缺一即拒绝出结论，不做「降级凑数」）：

1. 本地 vipdoc 存在且能定位到最新已收盘交易日；
2. 数据滞后超过 `stale_days_max` 个自然日 → 直接报错，让用户去通达信下载盘后数据；
3. 当日无有效快照（节假日 / 未下载）→ 报错而不是返回空候选。
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from app.adapters.tdx_daily import TdxDailyError, TdxDailySource, asof
from app.services.banqiang import (
    STATE_GROUPS,
    UNVERIFIABLE,
    build_series,
    funnel_key,
    score_all,
    scan_one,
    state_of,
)

WORKERS = 8


class BanqiangError(RuntimeError):
    """数据不可用（目录缺失 / 数据过期 / 当日无快照）。"""


@dataclass
class BanqiangParams:
    """扫描参数。阈值改这里就要同步 config.yaml 与 references/strategy.md。"""

    min_bars: int = 30                # 少于 30 根日线算不出板枪结构
    tail_bars: int = 260              # 每股读最近 N 根（「500 里高量」要 250 根）
    board_min_days: int = 60          # 上市不足 60 个自然日：量能不可比，剔除
    max_candidates: int = 120         # 候选表上限（按评分排序后截断）
    stale_days_max: int = 10          # 全市场数据滞后上限
    limit: int = 0                    # 调试用：只扫前 N 只

    @classmethod
    def from_config(cls, config: dict) -> "BanqiangParams":
        section = (config or {}).get("banqiang") or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in section.items() if k in known and v is not None})

    def summary(self) -> dict:
        return {
            "日线根数下限": self.min_bars,
            "每股读取根数": self.tail_bars,
            "次新过滤（自然日）": self.board_min_days,
            "候选上限": self.max_candidates,
            "数据滞后上限（自然日）": self.stale_days_max,
        }


@dataclass
class ScanResult:
    data_date: int | None = None
    newest_date: int | None = None
    stale_days: int | None = None
    universe: int = 0
    scanned: int = 0
    errors: int = 0
    groups: dict = field(default_factory=dict)       # state -> [item]
    picks: list = field(default_factory=list)        # 买点已触发 + 买点临近
    funnel: dict = field(default_factory=dict)       # 漏斗：哪一层筛掉多少
    sector_caliber: str = ""
    elapsed: float = 0.0
    source_label: str = ""
    params: dict = field(default_factory=dict)
    scanned_at: str = ""
    notes: list = field(default_factory=list)
    frames: dict = field(default_factory=dict)       # symbol -> list[dict]，不落盘

    @property
    def counts(self) -> dict:
        return {state: len(self.groups.get(state) or []) for state in STATE_GROUPS}

    @property
    def all_candidates(self) -> list:
        return [item for state in STATE_GROUPS for item in (self.groups.get(state) or [])]

    def to_payload(self) -> dict:
        return {
            "data_date": self.data_date, "newest_date": self.newest_date,
            "stale_days": self.stale_days, "universe": self.universe,
            "scanned": self.scanned, "errors": self.errors,
            "groups": self.groups, "picks": self.picks, "funnel": self.funnel,
            "sector_caliber": self.sector_caliber, "elapsed": self.elapsed,
            "source_label": self.source_label, "params": self.params,
            "scanned_at": self.scanned_at, "notes": self.notes,
        }

    @classmethod
    def from_payload(cls, data: dict) -> "ScanResult":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        result = cls(**{k: v for k, v in (data or {}).items() if k in known})
        result.frames = {}          # 日线不进 JSON，选中时按需回取
        return result


def stale_days_of(date: int | None, now: datetime | None = None) -> int | None:
    if not date:
        return None
    moment = now or datetime.now()
    try:
        target = datetime.strptime(str(date), "%Y%m%d")
    except ValueError:
        return None
    return (moment.date() - target.date()).days


def _sector_ranks(source: TdxDailySource, target: int) -> tuple[dict, str]:
    """行业强度排名：rank 用当日涨幅排序，ret5 给 5 日涨幅。缺指数时返回空表。"""
    strength = source.sector_strength(target) or {}
    ranked = sorted(
        strength.items(),
        key=lambda kv: kv[1].get("sector_change")
        if kv[1].get("sector_change") is not None else -99,
        reverse=True,
    )
    total = len(ranked)
    ranks = {tcode: (index + 1, total) for index, (tcode, _) in enumerate(ranked)}
    caliber = (f"通达信 880xxx 行业指数（{total} 个）" if total
               else "行业强度不可得（缺 880xxx 指数日线），板块维度按最低分处理")
    return {"ranks": ranks, "strength": strength}, caliber


def _reason_text(ctx: dict) -> str:
    """一句话说清这只票现在处在什么位置。"""
    s2, s3, s5, tg = ctx.get("s2"), ctx.get("s3"), ctx.get("s5"), ctx.get("tg") or {}
    if ctx.get("early"):
        return f"板日 {ctx['board_date']} 涨停，枪日还没出来"
    state = ctx.get("state") or ""
    head = (f"板日 {ctx['board_date']} → 枪日 {ctx['gun_date']}（{s2['gun_type']} "
            f"{s2['gun_grade']} 级）→ 回调 {s3['pull_days']} 日")
    if s5["buy_triggered"]:
        tail = f"买点已触发：{s5['buy_type']}，触发价 {s5['buy_trigger_price']:.2f}"
    elif s5["buy_near"]:
        tail = f"买点临近：{s5['buy_type']} 触发价 {s5['buy_trigger_price']:.2f}"
    else:
        tail = f"等买点：{s5['buy_type']} 触发价 {s5['buy_trigger_price']:.2f}"
    stop = tg.get("stop")
    return f"{head} · {state} · {tail}" + (f" · 止损 {stop:.2f}" if stop else "")


def _build_item(ctx: dict, meta: dict, sector: dict | None) -> dict:
    """规则引擎的 ctx + 行情元数据 → 界面直接可用的候选条目。"""
    item = dict(ctx)
    item["symbol"] = meta["symbol"]
    item["code"] = meta["code"]
    item["name"] = meta.get("name") or ctx.get("name") or meta["code"]
    item["market"] = meta["market"]
    item["board"] = meta.get("board") or ""
    item["industry"] = meta.get("industry") or "未分类"
    item["sector"] = sector or {}
    item["plan"] = ctx.get("tg") or {}
    if ctx.get("early"):
        item["score"] = {"total": None, "grade": "", "dims": {}, "veto": [],
                         "notes": ["枪日未出现，五阶段还没走完，暂不评分"]}
    else:
        item["score"] = score_all(ctx)
    item["reason"] = _reason_text(ctx)
    item["state"] = ctx.get("state") or state_of(ctx)
    item["stage_label"] = _stage_label(ctx)
    return item


def _stage_label(ctx: dict) -> str:
    """五阶段走到哪一步：S1 板日 / S2 枪日 / S3 回调 / S5 买点。"""
    s5 = ctx.get("s5")
    if ctx.get("early") or not s5:
        return "S1 板日"
    if s5["buy_triggered"]:
        return "S5 买点"
    return "S3 回调"


def scan(source: TdxDailySource, params: BanqiangParams, date: int | None = None,
         on_progress=None, should_stop=None) -> ScanResult:
    """收盘后（EOD）口径的全市场扫描。`date` 只用于历史回放，缺省取最新定稿交易日。"""
    started = time.time()
    calendar = source.calendar()
    if not calendar:
        raise BanqiangError("读不到交易日历（vipdoc/sh/lday/sh000001.day 缺失或为空）")
    latest = calendar[-1]
    target = int(date) if date else latest
    if target not in calendar:
        raise BanqiangError(f"{target} 不在本地数据覆盖范围（最新 {latest}）")

    stale = stale_days_of(target)
    if target == latest and stale is not None and stale > params.stale_days_max:
        raise BanqiangError(
            f"本地日线最新只到 {target}，已滞后 {stale} 个自然日（上限 {params.stale_days_max}）。\n"
            "请先打开通达信客户端 → 盘后数据下载，再重新扫描。\n"
            "过期数据不做选股，这是硬性的时间有效性校验。")

    notes = [
        f"数据口径：本地通达信日线（{source.root}），收盘后定稿，价格为不复权",
        *UNVERIFIABLE,
        "只输出选股与信号，不自动下单",
    ]
    if target != latest:
        notes.append(f"历史回放模式：目标日 {target}，本地最新 {latest}，一律按 {target} 截断 K 线")
    if target == latest and datetime.now().strftime("%Y%m%d") != str(latest):
        notes.append(f"本地最新定稿日线为 {latest}（今日尚未收盘或未下载盘后数据）")

    universe = source.universe(date=target, min_list_days=params.board_min_days,
                               require_caps=False, limit=params.limit)
    if not universe:
        raise BanqiangError("股票池为空：检查 vipdoc 目录与 base.dbf 是否完整")

    sector_data, caliber = _sector_ranks(source, target)
    ranks = sector_data["ranks"]
    strength = sector_data["strength"]

    items: list = []
    funnel: dict = {}
    errors = 0
    total = len(universe)
    done = 0
    if on_progress:
        on_progress(0, total, "读本地日线")

    def work(meta: dict) -> dict | None:
        try:
            bars = asof(source.bars_list(meta, params.tail_bars), target)
        except (OSError, TdxDailyError, ValueError, KeyError):
            return {"__error__": True}
        if len(bars) < params.min_bars or not bars or bars[-1]["date"] != target:
            return {"__error__": True}
        try:
            ctx, reason = scan_one(build_series(bars), len(bars) - 1, meta["code"], meta["name"])
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return {"__error__": True}
        if not ctx:
            return {"__reason__": reason}
        info = strength.get((meta.get("sector") or {}).get("tcode") or "") or {}
        rank = ranks.get((meta.get("sector") or {}).get("tcode") or "")
        sector = {
            "name": (meta.get("sector") or {}).get("name") or meta.get("industry") or "未分类",
            "rank": rank[0] if rank else None,
            "total": rank[1] if rank else None,
            "ret5": info.get("sector_change_5d"),
            "change": info.get("sector_change"),
        }
        return _build_item(ctx, meta, sector)

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for result in executor.map(work, universe):
            done += 1
            if result is None:
                errors += 1
            elif result.get("__error__"):
                errors += 1
            elif "__reason__" in result:
                key = funnel_key(result["__reason__"])
                funnel[key] = funnel.get(key, 0) + 1
            else:
                items.append(result)
            if on_progress and (done % 200 == 0 or done == total):
                on_progress(done, total, "五阶段识别")
            if should_stop and should_stop():
                raise BanqiangError("扫描已取消")
    if not items and done and not errors:
        raise BanqiangError(f"{target} 没有形成任何板枪结构：本地日线可能未下载或全部停牌")

    # 评分高的在前；未评分的（枪日未出）按板日新鲜度排在后面
    items.sort(key=lambda it: (-(it["score"].get("total") or 0), -(it["board_date"] or 0),
                               it["symbol"]))
    if params.max_candidates and len(items) > params.max_candidates:
        funnel["超过候选上限被截断"] = len(items) - params.max_candidates
        items = items[: params.max_candidates]

    groups = {state: [it for it in items if it["state"] == state] for state in STATE_GROUPS}
    picks = [it for it in items if it["state"] in ("买入观察", "买点临近")]

    # 只给最靠前的一批留日K，避免上万根 K 线常驻内存
    frames: dict = {}
    for it in items[:40]:
        meta = {"symbol": it["symbol"], "code": it["code"], "market": it["market"]}
        try:
            frames[it["symbol"]] = asof(source.bars_list(meta, params.tail_bars), target)
        except (OSError, TdxDailyError, ValueError, KeyError):
            continue

    result = ScanResult(
        data_date=target,
        newest_date=latest,
        stale_days=stale,
        universe=len(universe),
        scanned=len(universe) - errors,
        errors=errors,
        groups=groups,
        picks=picks,
        funnel=dict(sorted(funnel.items(), key=lambda kv: -kv[1])),
        sector_caliber=caliber,
        elapsed=round(time.time() - started, 1),
        source_label=f"本地通达信日线 · {source.root}",
        params=params.summary(),
        scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        notes=notes,
        frames=frames,
    )
    if not items:
        result.notes.append("今日没有符合条件的标的，不降标准凑数——建议继续观察")
    return result


# ---------------------------------------------------------------- 线程封装

@dataclass
class ScanProgress:
    done: int
    total: int
    phase: str


@dataclass
class ScanDone:
    result: ScanResult | None
    error: str | None
    at: float


class BanqiangScanner(threading.Thread):
    """独立扫描线程：请求-执行-结果三段，进度 0.25 秒节流一次。"""

    def __init__(self, config: dict, repo=None, parent=None) -> None:
        super().__init__(daemon=True, name="banqiang-scanner")
        self.config = config or {}
        self.repo = repo
        self.params = BanqiangParams.from_config(config)
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending_date: int | None = None
        self._requested = False
        self._busy = False
        self.running = False
        self._last_emit = 0.0

    # ---------------------------------------------------------------- 控制
    def request_scan(self, date: int | None = None) -> None:
        if self.running:
            return
        self._pending_date = date
        self._requested = True
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def drain(self) -> list:
        out = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return out

    def _emit(self, event) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:            # 界面卡住时丢最旧的进度，不能让结果进不去
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
            except queue.Empty:
                pass

    def _on_progress(self, done: int, total: int, phase: str) -> None:
        now = time.time()
        if now - self._last_emit < 0.25 and done < total:
            return
        self._last_emit = now
        self._emit(ScanProgress(done=done, total=total, phase=phase))

    # ---------------------------------------------------------------- 主循环
    def run(self) -> None:
        self._busy = False
        while not self._stop.is_set():
            self._wake.wait(timeout=0.5)
            self._wake.clear()
            if self._stop.is_set():
                break
            if not self._requested:
                continue
            date = self._pending_date
            self._pending_date = None
            self._requested = False
            self._busy = True
            self.running = True
            self._emit(ScanProgress(done=0, total=0, phase="准备中"))
            try:
                source = TdxDailySource.from_config(self.config)
                result = scan(source, self.params, date=date,
                              on_progress=self._on_progress,
                              should_stop=self._stop.is_set)
                self._emit(ScanDone(result=result, error=None, at=time.time()))
            except Exception as exc:                 # noqa: BLE001 - 线程里必须兜住
                self._emit(ScanDone(result=None, error=str(exc), at=time.time()))
            finally:
                self._busy = False
                self.running = False
