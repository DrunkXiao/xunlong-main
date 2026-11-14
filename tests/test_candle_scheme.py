"""K 线配色方案：四条均线关系规则 + 边界（贴线 / 均线未成形 / 未知方案）。

只测纯函数 `candle_color`，不建 QWidget：项目其余单测也不依赖显示环境。
"""
from __future__ import annotations

from app.ui.charts import CANDLE_PURPLE, CANDLE_SCHEMES, candle_color
from app.ui.theme import COLORS

UP = COLORS["up"]
DOWN = COLORS["down"]


def test_scheme_registry_names():
    assert CANDLE_SCHEMES == {"change": "涨跌配色", "ma": "均线关系"}


def test_bullish_alignment_is_red():
    """收 > MA5 且 MA5 > MA20 → 红。"""
    assert candle_color(11.0, 10.5, 10.8, 10.2, "ma") == UP


def test_bearish_alignment_is_green():
    """收 < MA5 且 MA5 < MA20 → 绿。"""
    assert candle_color(10.0, 10.6, 10.4, 10.8, "ma") == DOWN


def test_price_below_ma5_but_ma5_above_ma20_is_purple():
    """收 < MA5 且 MA5 > MA20 → 紫（价跌破均线，但均线仍多头）。"""
    assert candle_color(10.5, 10.6, 10.8, 10.2, "ma") == CANDLE_PURPLE


def test_price_above_ma5_but_ma5_below_ma20_is_purple():
    """收 > MA5 且 MA5 < MA20 → 紫（价在均线上，但均线仍空头）。"""
    assert candle_color(11.0, 10.6, 10.8, 11.2, "ma") == CANDLE_PURPLE


def test_equality_counts_as_divergence_not_bullish():
    """取等不属于任何一条规则：不能悄悄算成多头红。"""
    assert candle_color(10.8, 10.6, 10.8, 10.2, "ma") == CANDLE_PURPLE, "收=MA5"
    assert candle_color(11.0, 10.6, 10.8, 10.8, "ma") == CANDLE_PURPLE, "MA5=MA20"


def test_early_bars_without_ma_fall_back_to_change_color():
    """MA20 未成形（前 19 根）退回涨跌色，而不是留空或猜一个方向。"""
    assert candle_color(11.0, 10.6, None, None, "ma") == UP
    assert candle_color(10.4, 10.6, 10.5, None, "ma") == DOWN


def test_change_scheme_ignores_moving_averages():
    """涨跌方案下，均线排列不影响颜色。"""
    assert candle_color(11.0, 10.6, 10.8, 11.2, "change") == UP
    assert candle_color(10.4, 10.6, 10.8, 10.2, "change") == DOWN


def test_unknown_scheme_falls_back_to_change():
    assert candle_color(11.0, 10.6, 10.8, 11.2, "nonsense") == UP
    assert candle_color(10.4, 10.6, 10.8, 10.2, "") == DOWN


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def test_colors_match_red_up_green_down_convention():
    """铁律 5：红涨绿跌，紫只用于分歧，不能拿来当涨或跌。

    这里校验「红分量占优 / 绿分量占优」而不是钉死某个色号：
    调色是允许的，把涨跌配色反过来是不允许的。
    """
    r, g, b = _rgb(UP)
    assert r > g and r > b, f"涨色 {UP} 必须是红系"
    r2, g2, b2 = _rgb(DOWN)
    assert g2 > r2 and g2 > b2, f"跌色 {DOWN} 必须是绿系"
    assert UP != DOWN
    assert CANDLE_PURPLE not in (UP, DOWN), "紫色不能当成涨或跌"
    assert CANDLE_PURPLE not in (UP, DOWN)
