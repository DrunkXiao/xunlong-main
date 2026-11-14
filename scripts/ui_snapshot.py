"""无界面渲染截图：验证真实数据下的界面布局（offscreen 平台）。

运行：cd F:/workcn/寻龙盯盘 && python scripts/ui_snapshot.py
输出：docs/screenshots/*.png

第 1~3 张用真实数据渲染：01/03 是 tqcenter 行情，**02 分时是东方财富真实分钟线**
（241 个点，最近一个交易日）。
第 4 张是成交/大单面板：改用合成采样渲染，顶栏会挂「演示数据（合成，非行情）」
横幅，仅用于检查版式，不代表任何真实成交；此时分时线程会先停掉，避免真实分钟线
把演示数据覆盖掉。
第 5 张是龙虎榜窗口（东财真实披露），第 6 张是强势回调选股页（本地通达信日线全市场扫描，
约 40 秒；截这张之前会把演示横幅清空，保证图上没有演示数据）。
第 7 张是涨停四阶选股页（同样是本地通达信日线，全市场约 3 秒；四阶段 + 门控评分 +
日K 与交易计划卡）。
第 8 张是均线配色方案（同一批真实日K，颜色改按收盘价与 MA5/MA20 的排列：红多头 /
绿空头 / 紫分歧），截完立刻切回涨跌配色，不影响后续截图。
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)   # 用 windows 平台才能拿到系统字体

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.db.models import Quote
from app.ui.main_window import MainWindow

OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

DEMO = ["600519.SH", "000001.SZ", "300750.SZ", "601318.SH",
        "002594.SZ", "600036.SH", "000858.SZ", "688981.SH", "601899.SH"]

def pump(app, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.2)


def feed_demo(window, symbol: str, base_price: float) -> None:
    """合成采样：顶栏有横幅标注，且不写入数据库，仅用于检查版式。"""
    from app.services.intraday import SamplePoint

    start = datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)
    plan = [
        (30, 42_000, "buy"), (60, 68_000, "buy"), (90, 51_000, "sell"),
        (120, 1_250_000, "buy"), (150, 380_000, "buy"), (180, 96_000, "sell"),
        (210, 3_400_000, "sell"), (240, 720_000, "buy"), (270, 1_180_000, "buy"),
        (300, 540_000, "sell"), (330, 88_000, "buy"), (360, 6_200_000, "buy"),
        (390, 460_000, "sell"), (420, 1_050_000, "sell"), (450, 320_000, "buy"),
        (480, 760_000, "buy"), (510, 95_000, "sell"), (540, 1_600_000, "buy"),
        (570, 410_000, "sell"), (600, 2_100_000, "buy"),
    ]
    price = base_price
    cum_volume = 0
    cum_amount = 0.0
    for idx, (slot, amount, direction) in enumerate(plan):
        seconds = slot * 6          # 30s 计划放大到 3 分钟一档，铺满分时横轴
        price = round(base_price * (1 + (idx % 6) * 0.0008), 2)
        volume = int(amount / price)
        cum_volume += volume
        cum_amount += amount
        outside = int(cum_volume * (0.62 if direction == "buy" else 0.38))
        inside = cum_volume - outside
        quote = Quote(
            symbol=symbol, name="演示", price=price, prev_close=base_price,
            volume=cum_volume, amount=cum_amount, inside_volume=inside,
            outside_volume=outside, data_level="A", source="demo",
            received_at=start + timedelta(seconds=seconds),
        )
        point = window.sampler.feed(quote, start + timedelta(seconds=seconds))
        if point is not None:
            window.detector.feed(point, "A")
    window.detector.set_daily_range(symbol, base_price * 1.06, base_price * 0.92)
    # 再补两条位置明确的演示大单，让「相对高位/低位」在表里可见
    for seconds, amount, direction, offset in ((630, 1_350_000.0, "buy", 1.05),
                                               (660, 980_000.0, "sell", 0.94)):
        window.detector.feed(SamplePoint(
            symbol=symbol, time=start + timedelta(seconds=seconds),
            price=round(base_price * offset, 2), avg_price=base_price,
            interval_volume=int(amount / base_price), interval_amount=amount,
            cumulative_volume=cum_volume, cumulative_amount=cum_amount,
            direction=direction, quality_flag="sampled", interval_span_sec=30.0), "A")


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow(str(ROOT / "config.yaml"))
    window.setAttribute(Qt.WA_DontShowOnScreen, True)   # 不抢占桌面焦点
    window.resize(1680, 980)
    window.show()

    print("等待股票池与首轮行情…")
    pump(app, 14)

    for group in window.repo.groups():
        if group["name"] == "__截图演示__":
            window.repo.delete_group(group["id"])
    group_id = window.repo.create_group("__截图演示__")
    window.repo.add_symbols(group_id, DEMO)
    window._refresh_groups(group_id, DEMO[0])
    pump(app, 10)

    window._set_mode("single")
    window._on_period_changed("1d")
    pump(app, 8)
    window.grab().save(str(OUT / "01_single_daily.png"))
    print("已保存 01_single_daily.png")

    # 均线配色：同一根 K 线的颜色改由「收盘价 / MA5 / MA20」的排列决定
    # 红=多头排列、绿=空头排列、紫=价格与均线方向不一致（分歧）
    window.single_view.scheme.setCurrentIndex(window.single_view.scheme.findData("ma"))
    pump(app, 3)
    window.grab().save(str(OUT / "08_kline_ma_scheme.png"))
    print("已保存 08_kline_ma_scheme.png（均线配色：红多头 / 绿空头 / 紫分歧）")
    window.single_view.scheme.setCurrentIndex(window.single_view.scheme.findData("change"))
    pump(app, 2)

    window._on_period_changed("intraday")
    pump(app, 8)
    window.grab().save(str(OUT / "02_single_intraday.png"))
    got = len(window.minute_feed.series(window.current_symbol))
    print(f"已保存 02_single_intraday.png（东财分钟线 {got} 个点）")

    window._set_mode("grid")
    window.grid_combo.setCurrentIndex(3)      # 9 格
    pump(app, 8)
    window.grab().save(str(OUT / "03_grid_9.png"))
    print("已保存 03_grid_9.png")

    # ---------------- 第二阶段：成交 / 区间大单面板（合成演示数据）
    window.poller.stop()
    if window.intraday_poller is not None:
        window.intraday_poller.stop()      # 否则真实分钟线会把下面的演示数据顶掉
    symbol = DEMO[0]
    cached = window.repo.cached_quotes([symbol]).get(symbol) or {}
    window.show_demo_banner("演示数据（合成，非行情）· 仅检查版式")
    feed_demo(window, symbol, float(cached.get("price") or 100.0))
    window._set_mode("single")
    window._on_period_changed("intraday")
    window.trade_panel.set_source_mode("sampled")     # 演示的是采样口径，标题要跟着改
    window.trade_panel.set_points(window.sampler.series(symbol))
    window.trade_panel.set_orders(window.detector.orders(symbol))
    window._refresh_intraday(symbol)
    points = window.sampler.series(symbol)
    if points:
        window._focus_time(points[len(points) // 2].time)
    window.trade_panel.tabs.setCurrentIndex(1)   # 展示「区间大单」页
    pump(app, 4)
    window.grab().save(str(OUT / "04_trade_panel_demo.png"))
    print("已保存 04_trade_panel_demo.png（合成演示数据，顶栏有横幅标注）")

    # ---------------- 龙虎榜窗口（真实披露数据；离线时跳过，不留假截图）
    if window.lhb_adapter is not None:
        try:
            window._open_lhb()
            pump(app, 6)
            dialog = window._lhb_dialog
            if dialog is not None and dialog.board_table.rowCount() > 0:
                dialog.grab().save(str(OUT / "05_lhb_dialog.png"))
                print(f"已保存 05_lhb_dialog.png（{dialog.board_table.rowCount()} 条真实披露数据）")
            else:
                print("跳过 05_lhb_dialog.png：龙虎榜无可用数据")
        except Exception as exc:      # noqa: BLE001
            print(f"跳过 05_lhb_dialog.png：{type(exc).__name__}: {exc}")
    else:
        print("跳过 05_lhb_dialog.png：龙虎榜数据源未启用")

    # ---------------- 强势回调（真实本地通达信日线，独立线程全市场扫描）
    if hasattr(window, "pullback_view"):
        window.show_demo_banner("")          # 上一节的演示横幅不能留到真实数据截图上
        window._set_mode("pullback")
        pump(app, 2)
        before = id(window.pullback_view.result)   # 可能已从 app_settings 恢复上次结果
        window.pullback_view.start_scan()
        deadline = time.time() + 240
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.3)
            if not window.pullback_view.scanner.running and id(window.pullback_view.result) != before:
                break
        pump(app, 10)                        # 等选中候选的日K回填（tqcenter get_kline）
        result = window.pullback_view.result
        if result is not None and result.picks:
            window.grab().save(str(OUT / "06_pullback.png"))
            print(f"已保存 06_pullback.png（数据日 {result.data_date}，评估 {result.scanned} 只，"
                  f"候选 {len(result.picks)} 只，K线 {len(result.frames or {})} 只）")
        else:
            print("跳过 06_pullback.png：扫描无候选")
    else:
        print("跳过 06_pullback.png：强势回调视图未装配")

    # ---------------- 涨停四阶（真实本地通达信日线，独立线程全市场扫描）
    # 只读 .day，不占 tqcenter 会话，全市场约 3 秒；扫描完还要等选中标的的日K回填。
    if hasattr(window, "limitup_view"):
        window.show_demo_banner("")          # 千万别把演示横幅带进真实数据截图
        window._set_mode("limitup")
        pump(app, 2)
        before = id(window.limitup_view.result)
        window.limitup_view.start_scan()
        deadline = time.time() + 180
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.3)
            if (not window.limitup_view.scanner.running
                    and id(window.limitup_view.result) != before):
                break
        pump(app, 10)                        # 等日K回填与计划卡渲染
        limitup = window.limitup_view.result
        if limitup is not None and (limitup.picks or limitup.all_candidates):
            window.grab().save(str(OUT / "07_limitup.png"))
            counts = limitup.counts
            print(f"已保存 07_limitup.png（数据日 {limitup.data_date}，滞后 {limitup.stale_days} 天，"
                  f"扫描 {limitup.scanned} 只，S1 {counts['S1']}/S2 {counts['S2']}/"
                  f"S3 {counts['S3']}/S4 {counts['S4']}，精选 {len(limitup.picks)} 只）")
        else:
            print("跳过 07_limitup.png：当日无候选")
    else:
        print("跳过 07_limitup.png：涨停四阶视图未装配")

    window.repo.delete_group(group_id)
    # 还原到单股日K，避免把截图用的临时状态留给下次启动
    window._set_mode("single")
    window._on_period_changed("1d")
    window.current_symbol = ""
    window._refresh_groups()
    window.close()
    print("完成，已清理演示分组并还原界面状态")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
