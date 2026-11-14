"""涨停四阶段扫描：读本地日线 → 建快照 → 四阶段 → 评分 → 精选 → 池状态机。

跑在独立线程里（`LimitupScanner`），不排进 `BackgroundJobs` 的串行队列：
一次全市场扫描几秒到十几秒，排在切股任务前面会把界面堵住。

数据闸门（缺一即拒绝出结论，不做"降级凑数"）：

1. 本地 vipdoc 存在且能定位到最新已收盘交易日；
2. 数据滞后超过 `stale_days_max` 个自然日 → 直接报错，让用户去通达信下载盘后数据；
3. 当日无有效快照（节假日/未下载）→ 报错而不是返回空候选。
"""
from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from app.adapters.tdx_daily import TdxDailyError, TdxDailySource, asof
from app.services.limitup import (
    LimitupError,
    NEXT_TRIGGER,
    STAGE_LABEL,
    STAGE_TO_STATE,
    STATE_LABEL,
    build_plan,
    build_snapshot,
    evaluate_carry,
    evaluate_second_launch,
    limitup_checklist,
    market_environment,
    reason_text,
    risk_of,
    score,
    select_final,
    stage1_filter,
    stage2_confirm,
    aggregate_sectors,
)
from app.services.limitup_pool import LimitupPool

WORKERS = 8


@dataclass
class LimitupParams:
    """扫描参数。阈值改这里就要同步改 `references/strategy.md` 与 README。"""

    min_bars: int = 25
    tail_bars: int = 130
    board_min_days: int = 60          # 上市不足 60 个自然日：量能不可比，剔除
    top: int = 10                     # 精选上限
    stale_days_max: int = 10          # 全市场数据滞后上限，超过直接拒绝选股
    max_idle_days: int = 12           # 跟踪多久无信号转 S9
    limit: int = 0                    # 调试用：只扫前 N 只

    @classmethod
    def from_config(cls, config: dict) -> "LimitupParams":
        section = (config or {}).get("limitup") or {}
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in section.items() if k in known and v is not None})

    def summary(self) -> dict:
        return {
            "日线根数下限": self.min_bars,
            "每股读取根数": self.tail_bars,
            "次新过滤（自然日）": self.board_min_days,
            "精选上限": self.top,
            "数据滞后上限（自然日）": self.stale_days_max,
            "跟踪窗口（交易日）": self.max_idle_days,
        }


@dataclass
class ScanResult:
    data_date: int | None = None
    newest_date: int | None = None
    stale_days: int | None = None
    universe: int = 0
    scanned: int = 0
    errors: int = 0
    market: dict = field(default_factory=dict)
    indices: dict = field(default_factory=dict)
    amounts: dict = field(default_factory=dict)
    groups: dict = field(default_factory=dict)          # stage -> [candidate]
    picks: list = field(default_factory=list)
    pending: list = field(default_factory=list)         # S5 缩量整理，只给触发条件
    carry_stats: dict = field(default_factory=dict)
    launch_confirmed: list = field(default_factory=list)
    reject: dict = field(default_factory=dict)          # 三倍量未通过统计 + 明细
    pool_summary: dict = field(default_factory=dict)
    pool_text: str = ""
    review: dict = field(default_factory=dict)
    elapsed: float = 0.0
    source_label: str = ""
    params: dict = field(default_factory=dict)
    scanned_at: str = ""
    notes: list = field(default_factory=list)
    frames: dict = field(default_factory=dict)          # symbol -> list[dict]，不落盘

    @property
    def counts(self) -> dict:
        return {stage: len(self.groups.get(stage) or []) for stage in ("S1", "S2", "S3", "S4")}

    @property
    def all_candidates(self) -> list:
        return [c for stage in ("S4", "S3", "S2", "S1") for c in (self.groups.get(stage) or [])]

    def to_payload(self) -> dict:
        return {
            "data_date": self.data_date, "newest_date": self.newest_date,
            "stale_days": self.stale_days, "universe": self.universe,
            "scanned": self.scanned, "errors": self.errors,
            "market": self.market, "indices": self.indices, "amounts": self.amounts,
            "groups": self.groups, "picks": self.picks, "pending": self.pending,
            "carry_stats": self.carry_stats, "launch_confirmed": self.launch_confirmed,
            "reject": self.reject, "pool_summary": self.pool_summary,
            "pool_text": self.pool_text, "review": self.review,
            "elapsed": self.elapsed, "source_label": self.source_label,
            "params": self.params, "scanned_at": self.scanned_at, "notes": self.notes,
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


def _new_candidate(snap: dict, stage: str, carry: dict | None, launch: dict | None,
                   rec: dict | None, sector_rank, market: dict) -> dict:
    score_info = score(snap, stage, carry=carry, launch=launch,
                       sector_rank=sector_rank, market=market)
    plan = build_plan(snap, rec, launch, stage)
    # 首板日是全流程的基准：S1/S2 就是当天，S3/S4 取池里记录的首板日
    board_date = (rec or {}).get("first_board_date")
    board_close = (rec or {}).get("first_board_close")
    if stage in ("S1", "S2") or not board_date:
        board_date = snap["date"]
        board_close = snap["close"]
    return {
        "snap": snap,
        "stage": stage,
        "stage_label": STAGE_LABEL[stage],
        "state_label": STATE_LABEL.get(STAGE_TO_STATE[stage], ""),
        "first_board_date": int(board_date) if board_date else None,
        "first_board_close": board_close,
        "score": score_info,
        "plan": plan,
        "carry": carry,
        "launch": launch,
        "risk": risk_of(snap),
        "next_trigger": NEXT_TRIGGER[stage],
        "reason": reason_text(snap, stage, carry, launch),
        "checks": limitup_checklist(snap, stage),
    }


def scan(source: TdxDailySource, params: LimitupParams, pool: LimitupPool,
         date: int | None = None, on_progress=None, should_stop=None) -> ScanResult:
    """收盘后（EOD）口径的全市场扫描。`date` 只用于历史回放，缺省取最新定稿交易日。"""
    started = time.time()
    calendar = source.calendar()
    if not calendar:
        raise LimitupError("读不到交易日历（vipdoc/sh/lday/sh000001.day 缺失或为空）")
    latest = calendar[-1]
    target = int(date) if date else latest
    if target not in calendar:
        raise LimitupError(f"{target} 不在本地数据覆盖范围（最新 {latest}）")

    stale = stale_days_of(target)
    if target == latest and stale is not None and stale > params.stale_days_max:
        raise LimitupError(
            f"本地日线最新只到 {target}，已滞后 {stale} 个自然日（上限 {params.stale_days_max}）。\n"
            "请先打开通达信客户端 → 盘后数据下载，再重新扫描。\n"
            "过期数据不做选股，这是硬性的时间有效性校验。")

    notes: list[str] = [
        f"数据口径：本地通达信日线（{source.root}），收盘后定稿",
        "涨停质量、炸板次数、封单金额为日线代理或不可得，一律不推测",
        "收益与计划均按收盘价计算，不含滑点与手续费",
    ]
    if target != latest:
        notes.append(f"历史回放模式：目标日 {target}，本地最新 {latest}，一律按 {target} 截断 K 线")
    if target == latest and datetime.now().strftime("%Y%m%d") != str(latest):
        notes.append(f"本地最新定稿日线为 {latest}（今日尚未收盘或未下载盘后数据）")

    universe = source.universe(date=target, min_list_days=params.board_min_days,
                               require_caps=True, limit=params.limit)
    if not universe:
        raise LimitupError("股票池为空：检查 vipdoc 目录与 base.dbf 是否完整")

    strength = source.sector_strength(target) or {}
    snapshots: list[dict] = []
    errors = 0
    total = len(universe)
    done = 0
    if on_progress:
        on_progress(0, total, "读本地日线")

    def work(meta: dict) -> dict | None:
        try:
            bars = asof(source.bars_list(meta, params.tail_bars), target)
        except (OSError, TdxDailyError, ValueError):
            return None
        if len(bars) < params.min_bars or not bars or bars[-1]["date"] != target:
            return None
        try:
            return build_snapshot(meta, bars, target, strength)
        except (KeyError, TypeError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        for snap in executor.map(work, universe):
            done += 1
            if snap is None:
                errors += 1
            else:
                snapshots.append(snap)
            if on_progress and (done % 200 == 0 or done == total):
                on_progress(done, total, "读本地日线")
            if should_stop and should_stop():
                raise LimitupError("扫描已取消")
    if not snapshots:
        raise LimitupError(f"{target} 没有有效快照：本地日线可能未下载或全部停牌")

    # 行业指数缺该日数据时，用成分股等权聚合补齐板块维度（口径会写进报告）
    aggregated = aggregate_sectors(snapshots)
    for tcode, info in aggregated.items():
        strength.setdefault(tcode, info)
    for snap in snapshots:
        info = strength.get(snap.get("sector_tcode") or "")
        if info:
            snap["sector_change"] = info.get("sector_change")
            snap["sector_change_5d"] = info.get("sector_change_5d")
            snap["sector_amount"] = info.get("sector_amount")

    ranked = sorted(strength.items(),
                    key=lambda kv: kv[1].get("sector_change")
                    if kv[1].get("sector_change") is not None else -99,
                    reverse=True)
    sector_rank = {tcode: (index + 1, len(ranked)) for index, (tcode, _) in enumerate(ranked)}

    market = market_environment(snapshots, strength)
    market["universe"] = len(universe)
    index_based = len([v for v in strength.values()
                       if "880xxx" in str(v.get("caliber", ""))])
    market["sector_caliber"] = (
        f"通达信 880xxx 行业指数 {index_based} 个 + 成分股等权补齐 {len(strength) - index_based} 个"
        if index_based else f"成分股等权聚合（{len(strength)} 个行业，非官方指数）")
    indices, amounts = source.index_snapshot(target)

    by_symbol = {snap["symbol"]: snap for snap in snapshots}
    s1 = stage1_filter(snapshots)
    s2_passed, s2_rejected = stage2_confirm(s1)

    candidates: dict[str, dict] = {}

    def add(snap: dict, stage: str, carry: dict | None = None,
            launch: dict | None = None, rec: dict | None = None) -> dict:
        item = _new_candidate(snap, stage, carry, launch, rec,
                              sector_rank.get(snap.get("sector_tcode")), market)
        order = {"S1": 0, "S2": 1, "S3": 2, "S4": 3}
        old = candidates.get(snap["symbol"])
        if old is None or order[stage] > order[old["stage"]]:
            candidates[snap["symbol"]] = item
        return item

    # ---- 三/四阶段：从池里推进
    carry_stats = {"A": 0, "B": 0, "C": 0, "D": 0}
    launch_confirmed: list[str] = []
    if on_progress:
        on_progress(total, total, "承接与二次启动")
    for record in list(pool.active()):
        symbol = record["symbol"]
        snap = by_symbol.get(symbol)
        if snap is not None and snap.get("date") == target:
            pool.refresh_quote(symbol, snap, target)
        # 幂等：当日新建或当日已处理过的记录不再重复评估
        if record.get("first_board_date") == target or record.get("updated_date") == target:
            continue
        try:
            bars = asof(source.bars_list({"symbol": symbol, "code": record.get("code"),
                                          "market": record.get("market")},
                                         params.tail_bars), target)
        except (OSError, TdxDailyError, ValueError):
            bars = []
        if not bars or snap is None:
            if bars and bars[-1]["date"] != target:
                pool.transition(symbol, "S9", target, "当日无行情（停牌/退市），退出观察")
            continue
        state = record.get("state")
        if state in ("S2", "S3"):
            carry = evaluate_carry(record, bars, snap)
            carry_stats[carry["carry_type"]] = carry_stats.get(carry["carry_type"], 0) + 1
            record["carry"] = carry
            if carry["carry_type"] == "D":
                pool.transition(symbol, "S7", target, carry["detail"])
            else:
                pool.transition(symbol, "S4", target, carry["detail"])
                add(snap, "S3", carry=carry, rec=record)
        elif state in ("S4", "S5"):
            launch = evaluate_second_launch(record, bars, snap)
            record["launch"] = launch
            if launch["confirmed"]:
                pool.transition(symbol, "S6", target, launch["detail"])
                launch_confirmed.append(symbol)
                add(snap, "S4", carry=record.get("carry"), launch=launch, rec=record)
            elif launch.get("expired"):
                pool.transition(symbol, "S8", target, launch["detail"])
            else:
                pool.transition(symbol, "S5", target, launch["detail"])

    # 同日重跑：本日已处理的 S4/S6 记录被幂等跳过，仍要在第四组体现（不改状态）
    for record in pool.by_state("S4", "S6"):
        symbol = record["symbol"]
        if symbol in candidates or record.get("updated_date") != target:
            continue
        snap = by_symbol.get(symbol)
        if snap is None:
            continue
        if record.get("state") == "S4":
            add(snap, "S3", carry=record.get("carry"), rec=record)
        else:
            add(snap, "S4", carry=record.get("carry"), launch=record.get("launch"), rec=record)

    for snap in s2_passed:
        add(snap, "S2")
    for snap in s1:
        add(snap, "S1")

    pool.retire_stale(target, max_idle_days=params.max_idle_days, trade_dates=calendar)

    # ---- 评分排序与精选
    items = list(candidates.values())
    picks = select_final(items, limit=params.top)
    groups = {stage: [c for c in items if c["stage"] == stage] for stage in ("S1", "S2", "S3", "S4")}
    for group in groups.values():
        group.sort(key=lambda c: -c["score"]["normalized"])

    # ---- 落库（S1/S2 写入池；S3/S4 已在上面 transition 过）
    for stage in ("S1", "S2"):
        for item in groups[stage]:
            pool.upsert(item["snap"], stage, item["score"], target, extra={
                "note": item["reason"],
                "next_trigger": NEXT_TRIGGER[stage],
                "plan": item["plan"],
            })
    pool.flush()

    pending = [r for r in pool.by_state("S5") if (r.get("launch") or {}).get("cons_high")]
    pending.sort(key=lambda r: -((r.get("score") or {}).get("normalized") or 0))

    reject_counts: dict[str, int] = {}
    for item in s2_rejected:
        for reason in item["reasons"]:
            key = reason.split("（")[0].split(" ")[0]
            reject_counts[key] = reject_counts.get(key, 0) + 1

    frames: dict[str, list[dict]] = {}
    keep = {c["snap"]["symbol"] for c in picks} | \
           {c["snap"]["symbol"] for c in groups["S3"] + groups["S4"]}
    for symbol in keep:
        snap = by_symbol.get(symbol)
        if snap is None:
            continue
        try:
            frames[symbol] = asof(source.bars_list(
                {"symbol": symbol, "code": snap["code"], "market": snap["market"]},
                params.tail_bars), target)
        except (OSError, TdxDailyError, ValueError):
            continue

    from app.services.limitup_pool import review_rows, review_stats
    try:
        rows = review_rows(pool, lambda s: source.bars_list(
            {"symbol": s, "code": s.split(".")[0], "market": s.split(".")[1]}, 130))
        review = review_stats(rows)
        review["rows"] = rows[:40]
    except Exception as exc:      # noqa: BLE001 - 复盘失败不影响当日选股
        review = {"error": f"{type(exc).__name__}: {exc}", "total": 0}

    result = ScanResult(
        data_date=target,
        newest_date=latest,
        stale_days=stale,
        universe=len(universe),
        scanned=len(snapshots),
        errors=errors,
        market=market,
        indices=indices,
        amounts=amounts,
        groups=groups,
        picks=picks,
        pending=pending,
        carry_stats=carry_stats,
        launch_confirmed=launch_confirmed,
        reject={"counts": reject_counts,
                "total": len(s2_rejected),
                "sample": s2_rejected[:40]},
        pool_summary=pool.summary(),
        pool_text=pool.summary_text(),
        review=review,
        elapsed=round(time.time() - started, 1),
        source_label=f"本地通达信日线 · {source.root}",
        params=params.summary(),
        scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        notes=notes,
        frames=frames,
    )
    if not picks:
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


class LimitupScanner(threading.Thread):
    """独立扫描线程：请求-执行-结果三段，进度 0.25 秒节流一次。"""

    def __init__(self, config: dict, repo, parent=None) -> None:
        super().__init__(daemon=True, name="limitup-scanner")
        self.config = config or {}
        self.repo = repo
        self.params = LimitupParams.from_config(config)
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending_date: int | None = None
        self._requested = False          # 单独标记：date=None 也是合法请求（取最新交易日）
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
        items = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items

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
                pool = LimitupPool(self.repo)
                result = scan(source, self.params, pool, date=date,
                              on_progress=self._on_progress,
                              should_stop=self._stop.is_set)
                self._emit(ScanDone(result=result, error=None, at=time.time()))
            except Exception as exc:                 # noqa: BLE001 - 线程里必须兜住
                self._emit(ScanDone(result=None, error=str(exc), at=time.time()))
            finally:
                self._busy = False
                self.running = False
