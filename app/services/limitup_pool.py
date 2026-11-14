"""涨停四阶段股票池：状态机 + 落库 + 复盘统计。

状态机（与 `references/strategy.md` 一一对应）：

    S1 首板发现 → S2 三倍量确认 → S3 等待次日承接 → S4 承接有效
    → S5 缩量整理 → S6 二次启动确认（终点）
    失败分支：S7 承接失败 / S8 突破失败 / S9 退出观察池 / S10 交易周期结束

两条必须守住的规矩：

1. **幂等**：同一天重跑不能把 S2 误判成承接失败——当日已处理的记录直接跳过。
2. **落库**：状态在 SQLite 里，重启后从原状态继续推进，不靠内存。

`review_stats()` 的收益统计是**首板日之后的收盘价对比**，不计交易费用与滑点，
也没考虑涨停买不进、T+1 不能卖——这些在返回值的 `caveat` 里写明，不粉饰。
"""
from __future__ import annotations

import statistics
from datetime import datetime

from app.services.limitup import STATE_LABEL, STAGE_TO_STATE, review_returns

ACTIVE_STATES = ("S1", "S2", "S3", "S4", "S5")
FINAL_STATES = ("S6", "S7", "S8", "S9", "S10")

# 界面上的展示顺序：越靠后越"成熟"
STATE_ORDER = ("S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10")

REVIEW_CAVEAT = ("收益按首板日收盘价与之后第 N 个交易日收盘价对比计算，"
                 "未计交易费用与滑点，也未考虑涨停买不进与 T+1 不能卖；"
                 "样本少于 20 条时不具统计意义，不要据此反复调参。")


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None and str(value) != "" else None
    except (TypeError, ValueError):
        return None


class LimitupPool:
    """跨日股票池。读一次、改内存、最后 flush 回库（一次扫描只写一次）。"""

    def __init__(self, repo) -> None:
        self.repo = repo
        self.records: dict[str, dict] = {}
        self.dirty: dict[str, dict] = {}
        self.load()

    # ---------------------------------------------------------------- 读写
    def load(self) -> None:
        self.records = {}
        for row in self.repo.limitup_pool():
            symbol = row.get("symbol")
            if not symbol:
                continue
            for key in ("first_board_date", "added_date", "updated_date", "last_date"):
                row[key] = _as_int(row.get(key))
            self.records[symbol] = row

    def flush(self) -> int:
        if not self.dirty:
            return 0
        written = self.repo.upsert_limitup_pool(list(self.dirty.values()))
        self.dirty.clear()
        return written

    def _touch(self, record: dict) -> dict:
        self.records[record["symbol"]] = record
        self.dirty[record["symbol"]] = record
        return record

    # ---------------------------------------------------------------- 查询
    def get(self, symbol: str) -> dict | None:
        return self.records.get(symbol)

    def active(self) -> list[dict]:
        return [r for r in self.records.values() if r.get("state") in ACTIVE_STATES]

    def by_state(self, *states: str) -> list[dict]:
        return [r for r in self.records.values() if r.get("state") in states]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records.values():
            state = record.get("state") or "S1"
            counts[state] = counts.get(state, 0) + 1
        return counts

    def summary_text(self) -> str:
        counts = self.summary()
        parts = [f"{STATE_LABEL.get(k, k)} {counts[k]}"
                 for k in STATE_ORDER if counts.get(k)]
        return " · ".join(parts) if parts else "股票池为空"

    # ---------------------------------------------------------------- 状态机
    def upsert(self, snap: dict, stage: str, score_info: dict, date: int,
               extra: dict | None = None) -> dict:
        """首板 / 三倍量阶段写入（S1、S2）；同一轮首板只重置一次基准。"""
        extra = extra or {}
        symbol = snap["symbol"]
        record = self.records.get(symbol)
        new_state = STAGE_TO_STATE[stage]
        if record is None:
            record = {
                "symbol": symbol,
                "code": snap["code"],
                "name": snap["name"],
                "market": snap.get("market"),
                "board": snap.get("board"),
                "sector": snap.get("sector"),
                "sector_tcode": snap.get("sector_tcode"),
                "first_board_date": snap["date"],
                "first_board_close": snap["close"],
                "first_board_volume": snap["volume"],
                "first_board_amount": snap["amount"],
                "first_board_turnover": snap.get("turnover"),
                "first_board_vol_ratio5": snap.get("vol_ratio5"),
                "added_date": date,
                "history": [],
            }
        elif stage in ("S1", "S2") and record.get("first_board_date") != date:
            # 新的一轮首板周期：基准必须重置，否则承接/突破会拿上一轮的价算
            record.update({
                "first_board_date": snap["date"],
                "first_board_close": snap["close"],
                "first_board_volume": snap["volume"],
                "first_board_amount": snap["amount"],
                "first_board_turnover": snap.get("turnover"),
                "first_board_vol_ratio5": snap.get("vol_ratio5"),
            })
        record.update({
            "state": new_state,
            "stage": stage,
            "name": snap["name"],
            "sector": snap.get("sector"),
            "updated_date": date,
            "last_date": snap["date"],
            "last_close": snap["close"],
            "last_volume": snap["volume"],
            "last_vol_ratio5": snap.get("vol_ratio5"),
            "last_turnover": snap.get("turnover"),
            "score": score_info,
            "risk_level": score_info.get("grade"),
            "next_trigger": extra.get("next_trigger"),
            "quality": snap.get("quality"),
            "quality_note": snap.get("quality_note"),
            "data_level": "C",
            "source": "tdx:vipdoc",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        })
        for key in ("carry", "launch", "plan"):
            if key in extra:
                record[key] = extra[key]
        history = record.setdefault("history", [])
        if not history or history[-1].get("date") != date or history[-1].get("state") != new_state:
            history.append({"date": date, "state": new_state,
                            "score": score_info.get("normalized"), "note": extra.get("note", "")})
        return self._touch(record)

    def refresh_quote(self, symbol: str, snap: dict, date: int | None = None) -> None:
        """只刷新最新行情，不动状态与评分（否则承接日会把 S2 覆盖成 S1）。"""
        record = self.records.get(symbol)
        if record is None or not snap:
            return
        record.update({
            "name": snap.get("name") or record.get("name"),
            "sector": snap.get("sector") or record.get("sector"),
            "last_date": snap.get("date"),
            "last_close": snap.get("close"),
            "last_volume": snap.get("volume"),
            "last_vol_ratio5": snap.get("vol_ratio5"),
            "last_turnover": snap.get("turnover"),
        })
        self._touch(record)

    def transition(self, symbol: str, state: str, date: int, note: str = "") -> dict | None:
        record = self.records.get(symbol)
        if record is None:
            return None
        if record.get("state") == state and record.get("updated_date") == date:
            return record
        record["state"] = state
        record["updated_date"] = date
        record.setdefault("history", []).append({
            "date": date, "state": state,
            "score": (record.get("score") or {}).get("normalized"), "note": note,
        })
        return self._touch(record)

    def retire_stale(self, date: int, max_idle_days: int = 12,
                     trade_dates: list[int] | None = None) -> list[str]:
        """首板次日仍未获量能确认、或跟踪超窗的记录转 S9。"""
        retired: list[str] = []
        trade_dates = trade_dates or []
        for record in list(self.records.values()):
            if record.get("state") not in ACTIVE_STATES:
                continue
            board_date = record.get("first_board_date")
            if not board_date:
                continue
            elapsed = len([d for d in trade_dates if board_date < d <= date]) if trade_dates else 0
            if record.get("state") == "S1" and elapsed >= 1:
                self.transition(record["symbol"], "S9", date,
                                "首板次日未获得三倍量确认，退出观察池")
                retired.append(record["symbol"])
            elif elapsed > max_idle_days:
                self.transition(record["symbol"], "S9", date,
                                f"跟踪 {elapsed} 个交易日无有效信号，退出观察池")
                retired.append(record["symbol"])
        return retired


# ---------------------------------------------------------------- 复盘统计

def review_rows(pool: LimitupPool, bars_provider) -> list[dict]:
    """给池内每条记录算首板后 T+1/2/3/5/10 收益。bars_provider(symbol) -> list[dict]。"""
    rows: list[dict] = []
    for record in pool.records.values():
        board_date = record.get("first_board_date")
        if not board_date:
            continue
        try:
            bars = bars_provider(record["symbol"]) or []
        except Exception:            # noqa: BLE001 - 单只读失败不影响整体复盘
            bars = []
        returns = review_returns(bars, int(board_date)) if bars else {}
        rows.append({
            "symbol": record["symbol"],
            "code": record.get("code"),
            "name": record.get("name"),
            "state": record.get("state"),
            "state_label": STATE_LABEL.get(record.get("state"), record.get("state")),
            "first_board_date": board_date,
            "score": (record.get("score") or {}).get("normalized"),
            "returns": returns,
        })
    rows.sort(key=lambda r: (r["first_board_date"] or 0, r["symbol"]), reverse=True)
    return rows


def review_stats(rows: list[dict]) -> dict:
    """按阶段汇总胜率与平均收益。样本不足时明确标注，不给"策略有效"的结论。"""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        buckets.setdefault(row.get("state_label") or "未知", []).append(row)

    def _aggregate(items: list[dict]) -> dict:
        out: dict[str, dict] = {}
        for horizon in ("t1", "t3", "t5"):
            values = [i["returns"].get(horizon) for i in items
                      if i.get("returns", {}).get(horizon) is not None]
            if not values:
                out[horizon] = {"n": 0, "win": None, "avg": None, "median": None,
                                "max": None, "min": None}
                continue
            out[horizon] = {
                "n": len(values),
                "win": round(len([v for v in values if v > 0]) / len(values) * 100, 1),
                "avg": round(sum(values) / len(values), 2),
                "median": round(statistics.median(values), 2),
                "max": round(max(values), 2),
                "min": round(min(values), 2),
            }
        return out

    by_state = {state: _aggregate(items) for state, items in buckets.items()}
    return {
        "total": len(rows),
        "evaluated": len([r for r in rows if (r.get("returns") or {}).get("t1") is not None]),
        "by_state": by_state,
        "overall": _aggregate(rows),
        "caveat": REVIEW_CAVEAT,
    }
