"""量学板枪选股：五阶段判定、回撤比口径、评分降档、买点与止损/目标位。

测试数据全部是**人工构造的日线**，不是真实行情。每条断言都对应
`references/strategy.md` 里的一条规则，失败时能直接定位到是哪条算错：

  * 涨停是双条件：涨幅落在板幅 ±0.35 个百分点 **且** 收盘 = 涨停价
  * 板日量 ≥ 前一日 1.30 倍，枪日量 ≥ 板日且 ≥ 5 日均量 1.20 倍
  * 回调首日必须缩量 ≥ 1/3，回调 ≤ 5 个交易日
  * 回撤比 ≥ 0.50 是**评级线**：0.25~0.50 是浅调变体，入池但降一档，< 0.25 淘汰
  * 止损 = 回调低点 ×(1−1%)，板日开盘是结构底线，两者不能混
  * 目标 1 = 板枪顶部（板日与枪日最高价的较大者）
  * 评分 8 个维度合计 = 100，任一维度不得超过上限
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.services.banqiang import (
    DIM_MAX,
    analyze,
    build_checks,
    build_series,
    funnel_key,
    grade_of,
    is_limit_up,
    scan_one,
    score_all,
    Series,
)
from app.services.banqiang_scanner import BanqiangParams, ScanResult

BASE_VOL = 1_000_000.0


def _dates(n: int, start: date = date(2026, 5, 4)) -> list[int]:
    out: list[int] = []
    day = start
    while len(out) < n:
        if day.weekday() < 5:
            out.append(int(day.strftime("%Y%m%d")))
        day += timedelta(days=1)
    return out


def _bars(rows: list[tuple]) -> list[dict]:
    """rows: (open, high, low, close, volume) → 日线 list[dict]（额 = 价 × 量）。"""
    dates = _dates(len(rows))
    out: list[dict] = []
    prev = rows[0][3]
    for i, (open_, high, low, close, volume) in enumerate(rows):
        out.append({
            "date": dates[i], "open": open_, "high": high, "low": low, "close": close,
            "volume": float(volume), "amount": close * float(volume),
            "pct": (close / prev - 1) * 100 if prev else 0.0, "pre_close": prev,
        })
        prev = close
    return out


def _flat(n: int = 70, price: float = 10.0, volume: float = BASE_VOL) -> list[tuple]:
    return [(price, price + 0.1, price - 0.1, price, volume) for _ in range(n)]


def _structure(pull: list[tuple] | None = None) -> list[dict]:
    """70 根横盘 + 板日 + 枪日 + 默认两日缩量回调（回撤比 0.571，深调）。"""
    rows = _flat()
    rows.append((10.5, 11.0, 10.45, 11.0, 1_600_000.0))    # 板日：收盘 = 涨停价
    rows.append((11.2, 11.9, 11.0, 11.4, 2_600_000.0))     # 枪日：阳枪 + 长上影 + 新高
    rows.extend(pull if pull is not None else [
        (11.35, 11.40, 11.15, 11.30, 1_100_000.0),
        (11.28, 11.30, 11.10, 11.25, 900_000.0),
    ])
    return _bars(rows)


def _analyze(bars: list[dict], code: str = "600000", name: str = "测试股"):
    series = build_series(bars)
    return scan_one(series, len(bars) - 1, code, name)


# ==================================================================== 板日口径

def test_limit_up_needs_close_at_limit_price():
    """只是涨了 5% 的低价股不算涨停；摸到涨停但没收上去算炸板，两者都不进板日。"""
    rows = _flat(70, 1.80, BASE_VOL)
    rows.append((1.82, 1.92, 1.82, 1.92, 1.6e6))       # +5.49%，不是 10% 板幅
    assert is_limit_up(build_series(_bars(rows)), 70, 0.10) is False

    rows = _flat(70, 10.0)
    rows.append((10.5, 11.0, 10.4, 10.92, 1.6e6))      # 摸了涨停 11.00，收盘 10.92
    series = build_series(_bars(rows))
    assert series.h[70] == 11.0 and series.c[70] == 10.92
    assert is_limit_up(series, 70, 0.10) is False

    rows[-1] = (10.5, 11.0, 10.45, 11.0, 1.6e6)        # 真正封板：收盘 = 涨停价
    assert is_limit_up(build_series(_bars(rows)), 70, 0.10) is True


def test_board_volume_must_expand():
    """板日量只有前一日 1.1 倍 → 量能未放大，不进候选。"""
    rows = _flat(70)
    rows.append((10.5, 11.0, 10.45, 11.0, 1_100_000.0))
    rows.append((11.2, 11.9, 11.0, 11.4, 2_600_000.0))
    rows.append((11.35, 11.40, 11.15, 11.30, 1_100_000.0))
    rows.append((11.28, 11.30, 11.10, 11.25, 900_000.0))
    ctx, reason = _analyze(_bars(rows))
    assert ctx is None and "量能未放大" in reason


# ==================================================================== 枪日

def test_gun_must_break_board_high():
    """枪日最高没超过板日最高 → 不算创新高。"""
    bars = _structure()
    bars[71].update(open=11.2, high=10.98, low=10.9, close=10.95, volume=2.6e6)
    ctx, reason = _analyze(bars)
    assert ctx is None and "未创新高" in reason


def test_gun_must_expand_volume():
    """枪日量低于板日量 → 未放量。"""
    bars = _structure()
    bars[71].update(volume=1.2e6)
    ctx, reason = _analyze(bars)
    assert ctx is None and "未放量" in reason


# ==================================================================== 回调

def test_pullback_first_day_must_shrink():
    """回调首日量 > 枪日 2/3 → 没缩量，淘汰。"""
    bars = _structure([(11.35, 11.40, 11.15, 11.30, 2_000_000.0),
                       (11.28, 11.30, 11.10, 11.25, 1_800_000.0)])
    ctx, reason = _analyze(bars)
    assert ctx is None and "首日未缩量" in reason


def test_pullback_over_five_days_rejected():
    """回调超过 5 个交易日 → 结构过期。

    `scan_one` 的板日窗口只有 5 日，第 6 根回调时板日已经滑出窗口，
    实际会归到「最近 5 日无涨停」；这里直接调 `analyze` 把第三阶段那条闸门测到。
    """
    pull = [(11.36, 11.40, 11.20, 11.30, 1.1e6)] * 6
    bars = _structure(pull)
    ctx, reason = analyze(build_series(bars), 70, len(bars) - 1, "600000", "测试股")
    assert ctx is None and "超过 5 个交易日" in reason

    ctx, reason = _analyze(bars)
    assert ctx is None and "无涨停" in reason, "板日滑出窗口后应按「无涨停」拒绝"


def test_ratio_below_min_rejected():
    """只回撤 20%：不是回调，是在追高 → 回撤比过低。"""
    bars = _structure([(11.72, 11.78, 11.66, 11.72, 1.1e6),
                       (11.68, 11.72, 11.62, 11.68, 0.9e6)])
    ctx, reason = _analyze(bars)
    assert ctx is None and "回撤比过低" in reason


def test_pull_low_breaks_board_open_rejected():
    """回调低点跌破板日开盘 → 结构失效。"""
    bars = _structure([(10.6, 10.7, 10.40, 10.50, 1.1e6)])
    ctx, reason = _analyze(bars)
    assert ctx is None and "跌破板日开盘" in reason


# ==================================================================== 完整结构

def test_full_structure_state_and_levels():
    ctx, reason = _analyze(_structure())
    assert reason == "" and ctx is not None
    assert ctx["state"] == "缩量回调中"
    assert ctx["board_date"] and ctx["gun_date"]
    assert ctx["s2"]["gun_type"] == "阳枪"
    assert ctx["s3"]["deep"] is True
    assert 0.55 < ctx["s3"]["ratio"] < 0.60

    plan = ctx["tg"]
    assert plan["stop"] == round(ctx["s3"]["pull_low"] * 0.99, 2)
    assert plan["stop"] < plan["deep_floor"] or plan["stop"] > plan["deep_floor"]
    assert plan["deep_floor"] == 10.5, "结构底线 = 板日开盘"
    assert plan["target1"] == 11.9, "目标 1 = 板枪顶部（枪日最高 11.9）"

    checks = build_checks(ctx)
    assert checks["board_limit"]["ok"] and checks["pull_ratio"]["ok"]
    assert checks["buy_trigger"]["ok"] is False, "买点没触发就不能写已触发"


def test_yang_cover_triggers_buy():
    """深调分支：前一日阴线，当日收阳盖过其实体 → 买入观察。"""
    bars = _structure([
        (11.35, 11.40, 11.15, 11.30, 1_100_000.0),
        (11.28, 11.30, 11.10, 11.25, 900_000.0),
        (11.15, 11.40, 11.12, 11.36, 1_000_000.0),
    ])
    ctx, reason = _analyze(bars)
    assert ctx is not None, reason
    assert ctx["s5"]["buy_type"] == "阳盖阴"
    assert ctx["s5"]["buy_triggered"] is True
    assert ctx["state"] == "买入观察"


def test_shallow_pullback_downgrades_grade():
    """回撤比 0.25~0.50：入池但标浅调变体，并且评级降一档。"""
    bars = _structure([(11.65, 11.70, 11.50, 11.60, 1.1e6),
                       (11.55, 11.60, 11.45, 11.50, 0.9e6)])
    ctx, reason = _analyze(bars)
    assert ctx is not None, reason
    assert ctx["ratio_ok"] is False
    assert ctx["s5"]["buy_type"] == "过枪的高量顶", "浅调走买点 B"

    score = score_all(ctx)
    assert any("降一档" in note for note in score["notes"])


def test_score_dimensions_sum_to_total():
    ctx, _ = _analyze(_structure())
    score = score_all(ctx)
    assert set(score["dims"]) == set(DIM_MAX)
    for key, dim in score["dims"].items():
        assert 0 <= dim["score"] <= DIM_MAX[key], f"{key} 超出上限 {DIM_MAX[key]}"
    assert sum(d["score"] for d in score["dims"].values()) == score["total"]
    assert score["total"] <= 100
    assert score["grade"] == grade_of(score["total"])
    for dim in score["dims"].values():
        assert dim["items"], "每个维度都要有可解释的明细"


# ==================================================================== 其他

def test_area_high_volume_flag_only_top3():
    """「500 里高量」按 250 日量能前 3 判定，第 4 名不算。"""
    ctx, _ = _analyze(_structure())
    assert ctx["s2"]["gun_vol_rank250"] == 1
    assert ctx["s2"]["high_vol_500"] is True


def test_funnel_key_merges_detail():
    assert funnel_key("3|回调-首日未缩量 1/3") == "回调-首日未缩量 1/3"
    assert funnel_key("2|枪日-未放量（低于板日量）") == "枪日-未放量"
    assert funnel_key("") == "未知"


def test_scan_result_payload_roundtrip():
    ctx, _ = _analyze(_structure())
    item = dict(ctx)
    item.update(symbol="600000.SH", code="600000", market="SH", name="测试股",
                industry="软件服务", board="沪主板", state=ctx["state"],
                score=score_all(ctx), plan=ctx["tg"], reason="测试", stage_label="S3 回调")
    result = ScanResult(data_date=20260101, groups={"缩量回调中": [item]}, picks=[],
                        funnel={"板日-最近 5 日无涨停": 4000}, params=BanqiangParams().summary())
    payload = json.loads(json.dumps(result.to_payload(), ensure_ascii=False))
    back = ScanResult.from_payload(payload)
    assert back.data_date == 20260101
    assert back.groups["缩量回调中"][0]["symbol"] == "600000.SH"
    assert back.frames == {}, "日K不进 JSON，恢复后必须为空"
    assert back.counts["缩量回调中"] == 1


def test_empty_series_safe():
    """0 根日线不能让规则引擎抛异常（扫描里会当作无效快照跳过）。"""
    series = Series(d=[], o=[], h=[], l=[], c=[], v=[], a=[])
    ctx, reason = scan_one(series, 0, "600000", "测试股")
    assert ctx is None and reason


# ==================================================================== 界面装配

@pytest.fixture(scope="module")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _ui_config(tmp_path) -> object:
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n"
        f"  url: sqlite:///{(tmp_path / 'ui.db').as_posix()}\n"
        "datasource:\n"
        "  name: tqcenter\n"
        "  poll_interval_sec: 1\n"
        "alerts:\n"
        "  require_trading: false\n",
        encoding="utf-8",
    )
    return config


def _item(ctx: dict) -> dict:
    item = dict(ctx)
    item.update(symbol="600000.SH", code="600000", market="SH", name="测试股",
                industry="软件服务", board="沪主板", state=ctx["state"],
                score=score_all(ctx), plan=ctx["tg"], reason="合成结构的测试标的",
                stage_label="S3 回调")
    return item


def test_topbar_has_banqiang_after_limitup(qapp, tmp_path):
    """新入口必须排在「涨停四阶」后面，且原有四个模块照旧可用。"""
    pytest.importorskip("PySide6")
    from app.ui.main_window import MainWindow
    from tests.fakes import FakeAdapter

    window = MainWindow(str(_ui_config(tmp_path)),
                        adapter=FakeAdapter(symbols=["600000.SH"]))
    try:
        bar = window.btn_limitup.parentWidget().layout()
        widgets = [bar.itemAt(i).widget() for i in range(bar.count())]
        assert widgets.index(window.btn_banqiang) == widgets.index(window.btn_limitup) + 1
        assert window.btn_banqiang.text() == "量学板枪"

        for mode, view in (("single", window.single_view), ("grid", window.grid_view),
                           ("pullback", window.pullback_view),
                           ("limitup", window.limitup_view),
                           ("banqiang", window.banqiang_view)):
            window._set_mode(mode)
            assert window.center.currentWidget() is view, f"{mode} 切换失败"
            assert window.mode == mode

        window._set_mode("banqiang")
        assert window.btn_banqiang.isChecked() and not window.btn_limitup.isChecked()
        assert window.right_panel.isHidden(), "选股页应把右栏收起，把宽度让给候选表"
        window._set_mode("single")
        assert not window.right_panel.isHidden(), "切回单股后右栏必须恢复"
    finally:
        window.close()


def test_view_renders_result_and_detail(qapp, tmp_path):
    """喂一份结果：表格、详情、逐项核、评分拆解、计划都要能填上，不能抛异常。"""
    pytest.importorskip("PySide6")
    from app.ui.banqiang_view import BanqiangView
    from app.db.repo import Repo
    from app.db.session import Database

    db = Database(tmp_path / "view.db")
    db.init_schema()
    repo = Repo(db)
    view = BanqiangView({}, repo)
    try:
        ctx, _ = _analyze(_structure())
        item = _item(ctx)
        result = ScanResult(data_date=20260101, scanned=1, universe=1,
                            groups={"缩量回调中": [item], "买入观察": [],
                                    "买点临近": [], "形态形成中": []},
                            picks=[], funnel={"板日-最近 5 日无涨停": 4000},
                            params=BanqiangParams().summary())
        view.set_result(result)
        view.combo.setCurrentIndex(1)          # 全部候选
        assert view.table.rowCount() == 1
        assert view.plan_labels["stop"].text() != "--"
        assert view.plan_labels["t1"].text() == "11.90"
        assert view.score_total.text().endswith(("S", "A", "B", "C", "D"))
        assert view.check_labels["board_limit"].text() == "✓"
        assert view.check_labels["buy_trigger"].text() == "✗"
        assert "最近 5 日无涨停" in view.lbl_funnel.text()

        # 状态切到「买入观察」时表里应当没东西，详情回到空态而不是残留上一只
        view.combo.setCurrentIndex(2)
        assert view.table.rowCount() == 0
        assert view.detail_title.text() == "未选择标的"
    finally:
        view.stop()
        view.deleteLater()
