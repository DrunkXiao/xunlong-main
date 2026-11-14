"""对话框：新增预警规则、批量导入代码。"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QPlainTextEdit, QSpinBox, QVBoxLayout,
)

from app.services.alert_service import RULE_LABELS

RULE_OPTIONS = [
    ("price_above", "价格上穿（≥）"),
    ("price_below", "价格下破（≤）"),
    ("change_pct_above", "涨幅达到（%≥）"),
    ("change_pct_below", "跌幅达到（%≤）"),
    ("near_limit_up", "距涨停 ≤（%）"),
    ("near_limit_down", "距跌停 ≤（%）"),
    ("volume_ratio_above", "量比 ≥"),
    ("turnover_rate_above", "换手率 ≥（%）"),
    ("amount_spike", "成交额突增（近20区间均值×倍数）"),
    ("amount_above", "区间成交额 ≥（万元）"),
    ("surge", "N 分钟内快速拉升（%）"),
    ("plunge", "N 分钟内快速下跌（%）"),
    ("large_order_single", "区间大单 ≥（万元）"),
    ("large_order_streak", "连续同向区间累计 ≥（万元）"),
]

# 金额类规则：界面按万元输入，落库前换算成元
AMOUNT_RULES = ("amount_above", "large_order_single", "large_order_streak")
INTRADAY_RULES_UI = ("amount_spike", "surge", "plunge")


class AlertRuleDialog(QDialog):
    def __init__(self, symbol: str, name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"新增预警 · {name or symbol}")
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.symbol_label = QLabel(f"{name} {symbol}")
        self.type_combo = QComboBox()
        for key, text in RULE_OPTIONS:
            self.type_combo.addItem(text, key)
        self.value = QDoubleSpinBox()
        self.value.setDecimals(2)
        self.value.setRange(0.01, 100000.0)
        self.value.setValue(1.0)
        self.value.setSingleStep(0.1)
        self.unit = QLabel("")
        self.unit.setProperty("role", "muted")
        self.level_combo = QComboBox()
        for level in ("P1", "P2", "P3", "P4"):
            self.level_combo.addItem(level, level)
        self.level_combo.setCurrentIndex(2)
        self.cooldown = QSpinBox()
        self.cooldown.setRange(10, 86400)
        self.cooldown.setValue(300)
        self.cooldown.setSuffix(" 秒")

        value_row = QHBoxLayout()
        value_row.setSpacing(6)
        value_row.addWidget(self.value, 1)
        value_row.addWidget(self.unit)

        form.addRow("股票", self.symbol_label)
        form.addRow("条件", self.type_combo)
        form.addRow("数值", value_row)
        form.addRow("等级", self.level_combo)
        form.addRow("冷却", self.cooldown)
        layout.addLayout(form)

        self.hint = QLabel("预警只做条件提示，不构成买卖建议。同一规则在冷却期内不会重复提示。")
        self.hint.setWordWrap(True)
        self.hint.setProperty("role", "muted")
        layout.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.type_combo.currentIndexChanged.connect(self._on_type_changed)
        self._on_type_changed()

    def _on_type_changed(self) -> None:
        rule_type = self.type_combo.currentData()
        if rule_type in ("near_limit_up", "near_limit_down"):
            self.value.setValue(1.0)
            self.value.setSingleStep(0.1)
            self.unit.setText("%")
        elif rule_type in ("change_pct_above", "change_pct_below", "turnover_rate_above"):
            self.value.setValue(5.0)
            self.unit.setText("%")
        elif rule_type == "volume_ratio_above":
            self.value.setValue(2.5)
            self.unit.setText("倍")
        elif rule_type == "amount_spike":
            self.value.setValue(3.0)
            self.unit.setText("倍")
        elif rule_type == "amount_above":
            self.value.setValue(300.0)
            self.unit.setText("万元")
        elif rule_type in ("surge", "plunge"):
            self.value.setValue(2.0)
            self.unit.setText("% / 3 分钟")
        elif rule_type == "large_order_single":
            self.value.setValue(300.0)
            self.unit.setText("万元（区间累计）")
        elif rule_type == "large_order_streak":
            self.value.setValue(1000.0)
            self.unit.setText("万元（区间累计）")
        else:
            self.value.setValue(10.0)
            self.unit.setText("元")
        self._on_hint(rule_type)

    def _on_hint(self, rule_type: str) -> None:
        if rule_type in AMOUNT_RULES:
            self.hint.setText(
                "金额按「采样区间累计成交额」判定，不是单笔成交。"
                "本机数据源无逐笔，事件会标记「区间累计」。预警只做条件提示。")
        elif rule_type in INTRADAY_RULES_UI:
            self.hint.setText(
                "基于快照采样区间计算，非逐笔数据；采样间隔即轮询间隔，"
                "非交易时段不会触发。预警只做条件提示。")
        else:
            self.hint.setText(
                "预警只做条件提示，不构成买卖建议。同一规则在冷却期内不会重复提示。")

    def payload(self) -> dict:
        rule_type = self.type_combo.currentData()
        value = float(self.value.value())
        if rule_type in AMOUNT_RULES:
            value *= 1e4        # 万元 -> 元
        return {
            "rule_type": rule_type,
            "value": value,
            "level": self.level_combo.currentData(),
            "cooldown_sec": int(self.cooldown.value()),
        }


class ImportSymbolsDialog(QDialog):
    """批量导入：粘贴代码或 CSV，每行/每逗号一条。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量导入股票到当前分组")
        self.resize(420, 320)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("支持 600000 / 600000.SH / 名称。每行一个，或用逗号、空格分隔。"))
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("600000\n000001\n贵州茅台")
        layout.addWidget(self.text)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def tokens(self) -> list[str]:
        raw = self.text.toPlainText().replace(",", "\n").replace("\t", "\n")
        return [part.strip() for part in raw.splitlines() if part.strip()]
