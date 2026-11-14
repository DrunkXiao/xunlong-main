"""离屏渲染「主力资金」副图（真实数据：600519 日K + stockdb 资金流）。"""
import os
import sqlite3
import sys
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml  # noqa: E402
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from app.adapters.money_flow import MoneyFlowAdapter  # noqa: E402
from app.db.models import Bar  # noqa: E402
from app.ui.charts import CandleChart, MoneyFlowChart  # noqa: E402

SYMBOL = "600519.SH"

con = sqlite3.connect("data/dragon_hunter.db")
rows = con.execute(
    "select bar_time, open, high, low, close, volume, amount from kline_history "
    "where symbol=? and period='1d' order by bar_time", (SYMBOL,)).fetchall()
bars = [Bar(symbol=SYMBOL, period="1d", bar_time=datetime.fromisoformat(t),
            open=o, high=h, low=l, close=c, volume=v, amount=a or 0.0)
        for t, o, h, l, c, v, a in rows]

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
flow_rows = MoneyFlowAdapter(cfg).series(SYMBOL)
flow = {r.key: r.main_net for r in flow_rows}
jumbo = {r.key: r.jumbo_net for r in flow_rows}

app = QApplication(sys.argv)
root = QWidget()
root.resize(1180, 760)
layout = QVBoxLayout(root)
layout.setContentsMargins(6, 6, 6, 6)
layout.setSpacing(4)
candles = CandleChart()
candles.visible_count = 120
flow_chart = MoneyFlowChart()
flow_chart.bind(candles)
layout.addWidget(candles, 1)
layout.addWidget(flow_chart)

candles.set_bars(bars, "日K", "C")
flow_chart.set_flow(flow, "stockdb 资金流", "C", jumbo=jumbo)
flow_chart._hover_key = bars[-8].bar_time.strftime("%Y-%m-%d")
root.show()
app.processEvents()
root.grab().save("docs/money_flow_v2.png")
flow_chart.grab().save("docs/money_flow_v2_only.png")
print("saved, bars", len(bars), "flow", len(flow))
