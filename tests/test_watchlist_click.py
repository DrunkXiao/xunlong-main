"""自选股列表点击 → 单股精盯：跳转、参数传递、复用/换股、拖拽兼容性。"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FakeAdapter


@pytest.fixture(scope="module")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def _write_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        "database:\n"
        f"  url: sqlite:///{(tmp_path / 'click.db').as_posix()}\n"
        "datasource:\n"
        "  name: tqcenter\n"
        "  poll_interval_sec: 1\n"
        "intraday:\n"
        "  provider: snapshot\n"
        "alerts:\n"
        "  require_trading: false\n",
        encoding="utf-8",
    )
    return config


# ================================================================ 列表单击
def test_single_click_emits_symbol(qapp):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from app.ui.panels import QuoteTable

    table = QuoteTable()
    table.set_symbols(["600000.SH", "000001.SZ"], {"600000.SH": "浦发银行"})
    fired: list[str] = []
    table.open_requested.connect(fired.append)

    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier,
                     table.visualItemRect(table.item(1, 0)).center())

    assert fired == ["000001.SZ"]          # 点哪行就传哪只，参数不带歧义


def test_click_on_empty_area_does_nothing(qapp):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from app.ui.panels import QuoteTable

    table = QuoteTable()
    table.set_symbols(["600000.SH"])
    fired: list[str] = []
    table.open_requested.connect(fired.append)

    # 表格下方的空白处：无效索引，不能把「点空白」当成选中了某只股票
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier,
                     table.visualItemRect(table.item(0, 0)).bottomLeft() + QPoint(5, 60))

    assert fired == []


def test_drag_release_is_not_treated_as_click(qapp):
    """拖拽排序的释放不能顺手把单股页切走。"""
    pytest.importorskip("PySide6")
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent

    from app.ui.panels import QuoteTable

    table = QuoteTable()
    table.set_symbols(["600000.SH", "000001.SZ"])
    table.setDragEnabled(False)        # 只验证位移判定，不真的进 DnD 循环
    fired: list[str] = []
    table.open_requested.connect(fired.append)

    center = table.visualItemRect(table.item(0, 0)).center()

    def send(kind, point):
        return QMouseEvent(kind, QPointF(point),
                           QPointF(table.viewport().mapToGlobal(point)),
                           Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)

    table.mousePressEvent(send(QEvent.MouseButtonPress, center))
    # 挪出拖拽阈值：判定为「想拖」，不是「想点」
    table.mouseMoveEvent(send(QEvent.MouseMove, center + QPoint(0, 60)))
    table.mouseMoveEvent(send(QEvent.MouseMove, center))       # 又拖回原位
    table.mouseReleaseEvent(send(QEvent.MouseButtonRelease, center))

    assert fired == []


def test_group_panel_forwards_click_to_picked(qapp):
    pytest.importorskip("PySide6")
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from app.ui.panels import GroupPanel

    panel = GroupPanel()
    panel.table.set_symbols(["600000.SH"], {"600000.SH": "浦发银行"})
    fired: list[str] = []
    panel.picked.connect(fired.append)

    QTest.mouseClick(panel.table.viewport(), Qt.LeftButton, Qt.NoModifier,
                     panel.table.visualItemRect(panel.table.item(0, 0)).center())

    assert fired == ["600000.SH"]


# ================================================================ 跳转与复用
def test_open_symbol_switches_mode_and_reuses(qapp, tmp_path: Path):
    pytest.importorskip("PySide6")
    from app.ui.main_window import MainWindow

    window = MainWindow(str(_write_config(tmp_path)),
                        adapter=FakeAdapter(symbols=["600000.SH", "000001.SZ", "300750.SZ"]))
    try:
        window.repo.upsert_stocks([
            {"symbol": "600000.SH", "name": "浦发银行"},
            {"symbol": "000001.SZ", "name": "平安银行"},
            {"symbol": "300750.SZ", "name": "宁德时代"},
        ])
        window._refresh_groups(window.repo.groups()[0]["id"])
        window._add_symbols(["000001.SZ", "600000.SH"])
        window._set_mode("grid")

        calls: list[str] = []
        original = window._select_symbol
        window._select_symbol = lambda symbol: (calls.append(symbol), original(symbol))[1]

        # 1) 在多股页点自选股 → 切到单股精盯并加载该股
        #    加组时单股页默认载入第一只（000001.SZ），这里点另一只，确保走完整加载
        window.open_symbol("600000.SH")
        assert window.mode == "single"
        assert window.center.currentWidget() is window.single_view
        assert window.btn_single.isChecked()
        assert window._single_symbol == "600000.SH"
        assert window.current_symbol == "600000.SH"
        assert calls == ["600000.SH"]

        # 2) 再点同一只 → 复用，不重建图表、不重复请求
        window.open_symbol("600000.SH")
        assert calls == ["600000.SH"]

        # 3) 点另一只 → 换股，参数跟着换
        window.open_symbol("000001.SZ")
        assert calls == ["600000.SH", "000001.SZ"]
        assert window.current_symbol == "000001.SZ"

        # 4) 切到网格再点回已加载的那只 → 只切页面，不重载
        window._set_mode("grid")
        window.open_symbol("000001.SZ")
        assert calls == ["600000.SH", "000001.SZ"]
        assert window.mode == "single"
        assert window.center.currentWidget() is window.single_view
    finally:
        window.close()
