"""龙虎榜验收脚本（东方财富公开披露数据）。

运行：cd F:/workcn/寻龙盯盘 && python scripts/verify_lhb.py

验收项：
  1. 取到最近一个有榜单的交易日（周末/节假日不应被当成交易日）
  2. 当日榜单条数 > 0，且每条都带代码、原因、披露日、来源、等级 C
  3. 金额自洽：净买入 ≈ 买入额 − 卖出额
  4. 榜单按净买入降序返回（界面默认视图就是这个顺序）
  5. 个股历史可取，日期不晚于今天
  6. 买卖席位各 ≤5 条，机构净额由「机构专用」席位加总得到
  7. 落库幂等：重复写入不新增行；按日/按股/按日聚合查询一致
  8. 口径与文案不含身份推断词（主力/游资/庄家/建仓/出货）
  9. 网络异常抛 DataSourceError，不返回空列表冒充「当天没人上榜」

落库写在临时库 data/verify_lhb.db，跑完删除，不污染主库。
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.base import DataSourceError
from app.adapters.eastmoney_lhb import EastmoneyLhbAdapter
from app.config import load_config
from app.db.repo import Repo
from app.db.session import Database

PASS, FAIL = "通过", "未通过"
results: list[tuple[str, str, str]] = []
BANNED = ("主力", "游资", "庄家", "建仓", "出货", "洗盘")
TMP_DB = ROOT / "data" / "verify_lhb.db"


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name} — {detail}")


def main() -> int:
    config = load_config(str(ROOT / "config.yaml"))
    adapter = EastmoneyLhbAdapter(config)
    print(f"数据源：东方财富龙虎榜 · {adapter.source} · {adapter.data_level} 级\n")

    # 1. 最新交易日
    try:
        latest = adapter.latest_trade_date()
    except DataSourceError as exc:
        record("取最近交易日", False, f"网络失败：{exc.message}")
        latest = None
    else:
        ok = bool(latest)
        if ok:
            weekday = date.fromisoformat(latest).weekday()
            ok = weekday < 5 and date.fromisoformat(latest) <= date.today()
            record("取最近交易日", ok,
                   f"{latest}（周{'一二三四五六日'[weekday]}），今天是 {date.today().isoformat()}")
        else:
            record("取最近交易日", False, "接口没有返回日期")
    if not latest:
        return summarise()

    # 2. 当日榜单
    board = adapter.daily_board(latest)
    complete = all(r.get("symbol") and r.get("reason") and r.get("publish_date")
                   and r.get("source") and r.get("data_level") == "C" for r in board)
    record("当日榜单完整性", bool(board) and complete,
           f"{latest} 共 {len(board)} 条，原因/披露日/来源/等级齐备={complete}")

    # 3. 金额自洽
    bad = []
    for row in board:
        buy, sell, net = row.get("buy_amount"), row.get("sell_amount"), row.get("net_buy")
        if None in (buy, sell, net):
            continue
        if abs((buy - sell) - net) > 1.0:
            bad.append(row["symbol"])
    record("金额自洽（净买=买-卖）", not bad,
           "全部一致" if not bad else f"{len(bad)} 条不一致：{bad[:3]}")

    # 4. 排序
    nets = [r.get("net_buy") or 0 for r in board]
    ordered = all(nets[i] >= nets[i + 1] for i in range(len(nets) - 1))
    record("榜单按净买入降序", ordered,
           f"首 {nets[0] / 1e4:.0f}万 → 末 {nets[-1] / 1e4:.0f}万" if nets else "无数据")

    # 5. 个股历史
    if board:
        symbol = board[0]["symbol"]
        history = adapter.symbol_history(symbol, 20)
        dates = [h["trade_date"] for h in history]
        ok = bool(history) and all(d <= date.today().isoformat() for d in dates)
        record("个股历史上榜", ok,
               f"{symbol} {len(history)} 次，最近 {dates[0] if dates else '--'}")

        # 6. 席位 + 机构净额
        detail = adapter.fetch_detail(latest, symbol)
        buy, sell = detail["buy"], detail["sell"]
        inst = detail["institution_net"]
        inst_seats = [s for s in buy + sell if s.get("name") == "机构专用"]
        if inst is None:
            ok = not inst_seats
            note = "无「机构专用」席位 → 净额为空（不补 0）"
        else:
            manual = round(sum((s.get("buy") or 0) - (s.get("sell") or 0) for s in inst_seats), 2)
            ok = abs(manual - inst) < 1.0
            note = f"机构净额 {inst / 1e4:.0f}万，人工加总 {manual / 1e4:.0f}万"
        record("买卖席位与机构净额", len(buy) <= 5 and len(sell) <= 5 and ok,
               f"买 {len(buy)} 席 / 卖 {len(sell)} 席；{note}")

        # 7. 落库幂等与查询
        if TMP_DB.exists():
            TMP_DB.unlink()
        db = Database(TMP_DB, schema_path=ROOT / "app" / "db" / "schema.sql")
        db.init_schema()
        repo = Repo(db)
        first = repo.upsert_lhb(board)
        second = repo.upsert_lhb(board)
        count = repo.count_lhb()
        by_date = len(repo.lhb_by_date(latest))
        marks = repo.lhb_marks(symbol, 3650)
        ok = (first == len(board) and second == len(board) and count == len(board)
              and by_date == len(board) and latest in marks)
        record("落库幂等 + 按日/按股查询", ok,
               f"首次写 {first}、重写 {second}、库内 {count}、当日查询 {by_date}、"
               f"标记含当日={latest in marks}")

        # 席位落库后再写一次列表数据，不应把席位冲掉
        repo.upsert_lhb(detail["records"])
        repo.upsert_lhb(board)
        kept = repo.lhb_for_symbol(symbol, 5)
        kept_seats = next((len(r["buy_top5"] or []) for r in kept if r["trade_date"] == latest), 0)
        record("席位不被列表刷新覆盖", kept_seats == len(buy),
               f"席位保留 {kept_seats} 条（应有 {len(buy)} 条）")
        db.close() if hasattr(db, "close") else None
        TMP_DB.unlink(missing_ok=True)

    # 8. 文案口径
    hits: list[str] = []
    for name in ("app/adapters/eastmoney_lhb.py", "app/ui/lhb_dialog.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        hits += [f"{name}:{word}" for word in BANNED if word in text]
    record("无身份推断文案", not hits, "源码未出现主力/游资/庄家/建仓/出货" if not hits else str(hits))

    # 9. 异常不静默
    class Broken(EastmoneyLhbAdapter):
        def _request(self, params):
            raise DataSourceError(kind="network", message="模拟断网")

    try:
        Broken({"lhb": {"retries": 0}}).daily_board(latest)
    except DataSourceError as exc:
        record("断网时抛错而非静默空表", True, f"抛出 {exc.kind}：{exc.message[:36]}")
    else:
        record("断网时抛错而非静默空表", False, "返回了结果，说明异常被吞了")

    return summarise()


def summarise() -> int:
    print("\n==== 汇总 ====")
    for name, state, detail in results:
        print(f"{state}\t{name}\t{detail}")
    ok = sum(1 for _, state, _ in results if state == PASS)
    print(f"\n{ok}/{len(results)} 项通过")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
