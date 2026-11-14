"""龙虎榜（东方财富公开披露）测试：解析口径、落库幂等、查询与界面文案。

不联网：用假的 `_request` 顶掉 HTTP 层，喂固定的东财原样 JSON 片段。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.adapters.base import DataSourceError
from app.adapters.eastmoney_lhb import EastmoneyLhbAdapter
from app.db.repo import Repo
from app.db.session import Database

SCHEMA = Path(__file__).resolve().parent.parent / "app" / "db" / "schema.sql"


# ---------------------------------------------------------------- 原始数据样例
def daily_row(symbol="600519.SH", name="贵州茅台", day="2026-11-06", net=1.2e8,
              reason="日涨幅偏离值达到7%的前五只证券"):
    return {
        "TRADE_DATE": f"{day} 00:00:00",
        "SECUCODE": symbol,
        "SECURITY_CODE": symbol.split(".")[0],
        "SECURITY_NAME_ABBR": name,
        "EXPLANATION": reason,
        "BILLBOARD_NET_AMT": net,
        "BILLBOARD_BUY_AMT": 2.0e8,
        "BILLBOARD_SELL_AMT": 0.8e8,
        "CHANGE_RATE": 8.31,
        "CLOSE_PRICE": 1453.0,
        "TURNOVERRATE": 1.24,
        "TRADE_MARKET": "上交所主板",
        "D1_CLOSE_ADJCHRATE": 1.5,
        "D5_CLOSE_ADJCHRATE": -2.25,
        "D10_CLOSE_ADJCHRATE": None,
    }


def seat_row(name, buy=None, sell=None, trade_id="1"):
    return {"OPERATEDEPT_NAME": name, "BUY": buy, "SELL": sell,
            "NET": (buy or 0) - (sell or 0), "TRADE_ID": trade_id}


class FakeAdapter(EastmoneyLhbAdapter):
    """把 HTTP 换成固定响应，按 reportName 分发。"""

    def __init__(self, pages=None, buy=None, sell=None) -> None:
        super().__init__({"lhb": {"retries": 0, "timeout_sec": 1}})
        self.pages = pages if pages is not None else [[daily_row()]]
        self.buy = buy if buy is not None else [seat_row("机构专用", buy=6.6e7),
                                                seat_row("某证券营业部", buy=3.3e7)]
        self.sell = sell if sell is not None else [seat_row("某证券营业部", sell=2.2e7)]
        self.calls: list[dict] = []
        self._page_cursor = 0

    def _request(self, params: dict) -> dict:
        self.calls.append(params)
        report = params["reportName"]
        if report == "RPT_DAILYBILLBOARD_DETAILSNEW":
            if params.get("pageSize") == 1 and not params.get("filter"):
                return {"success": True, "result": {"data": [daily_row()]}}
            page = int(params.get("pageNumber", 1))
            rows = list(self.pages[min(page - 1, len(self.pages) - 1)])
            flt = params.get("filter") or ""
            if "TRADE_DATE='" in flt:          # 按请求日期改写，模拟真实按日过滤
                day = flt.split("TRADE_DATE='")[1][:10]
                rows = [{**row, "TRADE_DATE": f"{day} 00:00:00"} for row in rows]
            return {"success": True, "result": {"data": rows, "pages": len(self.pages)}}
        if report == "RPT_BILLBOARD_DAILYDETAILSBUY":
            return {"success": True, "result": {"data": self.buy}}
        if report == "RPT_BILLBOARD_DAILYDETAILSSELL":
            return {"success": True, "result": {"data": self.sell}}
        return {"success": True, "result": {"data": []}}


@pytest.fixture()
def repo(tmp_path):
    db = Database(tmp_path / "lhb.db", schema_path=SCHEMA)
    db.init_schema()
    return Repo(db)


# ==================================================================== 解析口径
def test_record_normalizes_units_and_level():
    adapter = FakeAdapter()
    records = adapter.daily_board("2026-11-06")
    assert len(records) == 1
    rec = records[0]
    assert rec["symbol"] == "600519.SH"
    assert rec["trade_date"] == "2026-11-06"
    assert rec["publish_date"] == "2026-11-06", "披露日必须落库"
    assert rec["data_level"] == "C", "披露数据固定 C 级，不得冒充实时"
    assert rec["source"] == "eastmoney:lhb"
    assert rec["net_buy"] == pytest.approx(1.2e8)
    assert rec["buy_amount"] == pytest.approx(2.0e8)
    assert rec["change_pct"] == pytest.approx(8.31)
    assert rec["post_perf"]["d1"] == pytest.approx(1.5)
    assert rec["post_perf"]["d10"] is None, "缺失的后验表现保持 None，不补 0"


def test_latest_trade_date_comes_from_api():
    adapter = FakeAdapter()
    assert adapter.latest_trade_date() == "2026-11-06"


def test_seats_and_institution_net():
    adapter = FakeAdapter()
    detail = adapter.fetch_detail("2026-11-06", "600519.SH")
    assert len(detail["buy"]) == 2 and len(detail["sell"]) == 1
    # 机构专用：买入 6600 万，卖出 0 → 净额 +6600 万；只是官方席位名加总
    assert detail["institution_net"] == pytest.approx(6.6e7)
    assert detail["buy"][0]["name"] == "机构专用"
    assert detail["buy"][0]["side"] == "buy"


def test_daily_board_pages_through_results():
    adapter = FakeAdapter(pages=[[daily_row(), daily_row("000001.SZ", "平安银行")],
                                 [daily_row("300750.SZ", "宁德时代")]])
    adapter.page_size = 2        # 满页才继续翻页，短页即最后一页（与真实接口一致）
    records = adapter.daily_board("2026-11-06")
    assert len(records) == 3
    assert {r["symbol"] for r in records} == {"600519.SH", "000001.SZ", "300750.SZ"}


def test_network_error_is_raised_not_silently_empty():
    class Broken(EastmoneyLhbAdapter):
        def _request(self, params):
            raise DataSourceError(kind="network", message="连接超时")

    with pytest.raises(DataSourceError):
        Broken({"lhb": {"retries": 0}}).daily_board("2026-11-06")


# ==================================================================== 落库
def test_upsert_is_idempotent_and_fills_stock_info(repo):
    adapter = FakeAdapter()
    records = adapter.daily_board("2026-11-06")
    assert repo.upsert_lhb(records) == 1
    assert repo.upsert_lhb(records) == 1     # 走 ON CONFLICT 更新，不新增行
    assert repo.count_lhb() == 1
    rows = repo.lhb_by_date("2026-11-06")
    assert len(rows) == 1
    assert rows[0]["symbol"] == "600519.SH"
    # 外键：stock_info 里必须已经补上这只票
    assert repo.stock_names(["600519.SH"])["600519.SH"] == "贵州茅台"


def test_seats_persist_without_overwriting_on_list_only_refresh(repo):
    adapter = FakeAdapter()
    repo.upsert_lhb(adapter.daily_board("2026-11-06"))
    detail = adapter.fetch_detail("2026-11-06", "600519.SH")
    repo.upsert_lhb(detail["records"])
    row = repo.lhb_for_symbol("600519.SH")[0]
    assert len(row["buy_top5"]) == 2
    assert row["institution_net"] == pytest.approx(6.6e7)

    # 再用「只有列表、没有席位」的数据刷新一次，席位不能被清空
    repo.upsert_lhb(adapter.daily_board("2026-11-06"))
    row2 = repo.lhb_for_symbol("600519.SH")[0]
    assert len(row2["buy_top5"]) == 2
    assert row2["institution_net"] == pytest.approx(6.6e7)


def test_marks_aggregate_same_day_reasons(repo):
    adapter = FakeAdapter()
    base = adapter.daily_board("2026-11-06")[0]
    repo.upsert_lhb([base, {**base, "reason": "日换手率达到20%的前五只证券", "net_buy": 3.0e7}])
    marks = repo.lhb_marks("600519.SH")
    mark = marks["2026-11-06"]
    assert mark["count"] == 2
    assert mark["net"] == pytest.approx(1.5e8)


def test_lhb_dates_and_symbol_history_order(repo):
    adapter = FakeAdapter()
    rows = adapter.daily_board("2026-11-04") + adapter.daily_board("2026-11-06")
    repo.upsert_lhb(rows)
    assert repo.lhb_dates(5) == ["2026-11-06", "2026-11-04"]
    history = repo.lhb_for_symbol("600519.SH", 10)
    assert [r["trade_date"] for r in history] == ["2026-11-06", "2026-11-04"]


# ==================================================================== 口径与文案
def test_no_identity_inference_in_source():
    """龙虎榜最容易滑向「机构进场/游资炒作」，源码里不许出现这类结论词。"""
    banned = ("主力", "游资", "庄家", "建仓", "出货", "洗盘", "拉升股价")
    root = Path(__file__).resolve().parent.parent
    for name in ("app/adapters/eastmoney_lhb.py", "app/ui/lhb_dialog.py"):
        text = (root / name).read_text(encoding="utf-8")
        hits = [word for word in banned if word in text]
        assert not hits, f"{name} 出现身份推断词：{hits}"


def test_dialog_shows_disclosure_source(qapp, repo):
    from app.ui.lhb_dialog import LhbDialog

    adapter = FakeAdapter()
    repo.upsert_lhb(adapter.daily_board("2026-11-06"))
    dialog = LhbDialog(adapter, repo, "600519.SH")
    assert dialog.board_table.rowCount() == 1
    row = [dialog.board_table.item(0, c).text() for c in range(7)]
    assert row[0] == "600519.SH"
    assert row[6] == "日涨幅偏离值达到7%的前五只证券"
    # 披露日不能省：标题行统一标注，行内 tooltip 也带
    assert "2026-11-06" in dialog.board_count.text()
    assert "披露" in dialog.board_count.text()
    dialog.deleteLater()


def test_dialog_works_without_adapter(qapp, repo):
    """数据源关掉时只展示库内历史，不崩也不假装能取数。"""
    from app.ui.lhb_dialog import LhbDialog

    repo.upsert_lhb(FakeAdapter().daily_board("2026-11-06"))
    dialog = LhbDialog(None, repo, "600519.SH")
    assert dialog.board_table.rowCount() == 1
    assert "未启用" in dialog.status.text()
    dialog.deleteLater()


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
