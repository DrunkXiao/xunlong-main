"""离屏渲染「分时 + 资金流」页（真实分钟线 + 参考图形状的合成资金流）。

资金流接口在本机被东财限流（连续探测后 RemoteDisconnected），所以资金流部分用
合成序列：五条线满足真实口径 `主力 = 超大单 + 大单`、`主力 + 中单 + 小单 = 0`，
起点都是 09:31 的 0，用来验证版式/图例/刻度/悬浮，不代表任何真实个股。
"""
import os
import random
import sqlite3
import sys
from datetime import datetime, timedelta

# 不用 QT_QPA_PLATFORM=offscreen：本机 Qt 6.12 的 offscreen 插件字体库为空，
# 中文会全部渲染成方块。WA_DontShowOnScreen 同样不弹窗，但走真实字体。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app.adapters.eastmoney_money_flow import MinuteFlowPoint  # noqa: E402
from app.services.intraday import points_from_sampled_bars  # noqa: E402
from app.db.models import Bar  # noqa: E402
from app.ui.views import SingleStockView  # noqa: E402

SYMBOL = "600519.SH"


def load_intraday():
    con = sqlite3.connect("data/dragon_hunter.db")
    rows = con.execute(
        "select symbol, bar_time, open, high, low, close, volume, amount, quality_flag "
        "from kline_history where period='1m' and bar_time like '2026-11-13%' "
        "group by symbol having count(*) > 200 limit 1").fetchall()
    symbol = rows[0][0] if rows else SYMBOL
    rows = con.execute(
        "select symbol, bar_time, open, high, low, close, volume, amount, quality_flag "
        "from kline_history where period='1m' and symbol=? and bar_time like '2026-11-13%' "
        "order by bar_time", (symbol,)).fetchall()
    bars = [Bar(symbol=r[0], period="1m", bar_time=datetime.fromisoformat(r[1]),
                open=r[2], high=r[3], low=r[4], close=r[5], volume=r[6],
                amount=r[7] or 0.0, quality_flag=r[8] or "em_minute")
            for r in rows]
    return symbol, points_from_sampled_bars(bars), bars[0].close if bars else 10.0


def synth_flow(day: datetime.date, seed: int = 7):
    """合成当日累计净额：主力=超大+大，主力+中+小≈0，形状参照附图。"""
    rng = random.Random(seed)
    points = []
    jumbo = big = mid = 0.0
    moment = datetime.combine(day, datetime.strptime("09:31", "%H:%M").time())
    end = datetime.combine(day, datetime.strptime("15:00", "%H:%M").time())
    while moment <= end:
        # 上午拉升、午后回落、尾盘再拉：分段给漂移，才看得出「资金流」的走势
        clock = moment.time()
        drift = 4.2e5 if clock < datetime.strptime("11:00", "%H:%M").time() else (
            -5.0e5 if clock < datetime.strptime("14:00", "%H:%M").time() else 2.4e5)
        jumbo += drift * rng.uniform(0.4, 1.5) + rng.gauss(0, 1.6e5)
        big += drift * rng.uniform(-0.6, 0.9) + rng.gauss(0, 1.3e5)
        mid += rng.gauss(0, 2.4e5)
        main = jumbo + big
        small = -(main + mid)
        points.append(MinuteFlowPoint(time=moment, main=main, small=small,
                                      mid=mid, big=big, jumbo=jumbo))
        moment += timedelta(minutes=1)
        if moment.time() == datetime.strptime("11:31", "%H:%M").time():
            moment = datetime.combine(day, datetime.strptime("13:01", "%H:%M").time())
    return points


def render(width: int, height: int, out: str, hover: bool = False, mode: str = "flow"):
    view = SingleStockView({"money_flow": {"enabled": False}})
    view.resize(width, height)
    symbol, points, prev_close = load_intraday()
    view.set_symbol(symbol)
    view.set_period("intraday")
    view.set_intraday(points, prev_close, None, None,
                      f"{symbol} · free-stockdb 本地库 1 分钟（非逐笔） · {len(points)}/241",
                      "C", source="minute")
    if mode == "flow":
        flow = synth_flow(points[-1].time.date())
        view.set_intraday_money_flow(flow, "东方财富分时资金流", "B")
        if hover:
            view.intraday_flow._hover_slot = 140
    elif mode == "empty":
        view.clear_intraday_money_flow("资金流加载中…")
    else:
        view.clear_intraday_money_flow(
            "东方财富分时资金流接口不可达：RemoteDisconnected: Remote end closed "
            "connection without response（已换 push2his 重试 1 次）")
    view.setAttribute(Qt.WA_DontShowOnScreen, True)
    view.show()
    QApplication.processEvents()
    view.grab().save(out)
    print("saved", out, "symbol", symbol, "points", len(points), "mode", mode)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    render(1180, 760, "docs/intraday_flow.png", hover=True)
    render(760, 640, "docs/intraday_flow_narrow.png")
    render(1180, 620, "docs/intraday_flow_error.png", mode="error")
