"""暗色交易主题常量。A股惯例：红涨绿跌，禁止欧美配色。"""
from __future__ import annotations

from pathlib import Path

COLORS = {
    "bg": "#000000",
    "panel": "#000000",
    "raised": "#1A1F27",
    "border": "#232A34",
    "text": "#E6EAF0",
    "text_dim": "#9AA4B2",
    "text_muted": "#5C6673",
    "up": "#ff0000",
    "down": "#4ce653",
    "warn": "#F5B544",
    "info": "#3D8BFD",
    "unknown": "#5C6673",
}

# QSS 用 CSS 字体列表；QFont() 只能吃单个 family，故另给单值常量
MONO_FONT = '"JetBrains Mono", "Consolas", monospace'
UI_FONT = '"Microsoft YaHei UI", "Segoe UI", sans-serif'
MONO_FAMILY = "Consolas"
UI_FAMILY = "Microsoft YaHei UI"


def color_for_change(value: float | None) -> str:
    """涨红、跌绿、平灰。"""
    if value is None:
        return COLORS["unknown"]
    if value > 0:
        return COLORS["up"]
    if value < 0:
        return COLORS["down"]
    return COLORS["text_dim"]


def load_qss() -> str:
    qss = Path(__file__).with_name("theme.css")
    return qss.read_text(encoding="utf-8") if qss.exists() else ""


def format_amount(value: float | None) -> str:
    """中文单位：万 / 亿。无数据返回 '--'，不显示 0。"""
    if value is None:
        return "--"
    if abs(value) >= 1e8:
        return f"{value / 1e8:.2f}亿"
    if abs(value) >= 1e4:
        return f"{value / 1e4:.2f}万"
    return f"{value:.0f}"


def format_pct(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:+.2f}%"
