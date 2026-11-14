"""用「资金流大小与累计同量级」的合成数据渲染副图，对照参考图的观感。"""
import os
import random
import sys
from datetime import date, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from app.db.models import Bar  # noqa: E402
from app.ui.charts import CandleChart, MoneyFlowChart  # noqa: E402


def trading_days(start: date, count: int):
    out, day = [], start
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


rnd = random.Random(11)
days = trading_days(date(2026, 5, 21), 120)
bars, flow, jumbo = [], {}, {}
price = 12.0
for i, day in enumerate(days):
    price = max(4.0, price + (0.06 if i > 78 else -0.02) + rnd.uniform(-0.25, 0.25))
    op = price - rnd.uniform(-0.2, 0.2)
    bars.append(Bar(symbol="002415.SZ", period="1d",
                    bar_time=datetime.combine(day, datetime.min.time()),
                    open=op, high=max(op, price) + rnd.uniform(0.03, 0.3),
                    low=min(op, price) - rnd.uniform(0.03, 0.3),
                    close=price, volume=rnd.randint(1_500_000, 6_000_000)))
    big = rnd.random() < 0.16
    base = rnd.uniform(-6e7, 5.5e7) if big else rnd.uniform(-8e6, 7e6)
    if i > 78:
        base += 9e6
    flow[day.strftime("%Y-%m-%d")] = base
    jumbo[day.strftime("%Y-%m-%d")] = base * rnd.uniform(0.5, 1.1) + rnd.uniform(-3e6, 3e6)

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
flow_chart._hover_key = bars[-20].bar_time.strftime("%Y-%m-%d")
root.show()
app.processEvents()
flow_chart.grab().save("docs/money_flow_v2_synth.png")
root.resize(760, 620)
app.processEvents()
flow_chart.grab().save("docs/money_flow_v2_narrow.png")
print("saved")
