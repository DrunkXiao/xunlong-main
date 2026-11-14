"""通达信联动：代码处理、URL 拼接、成功/失败文案。

不发真实请求到用户的桥接：用临时端口的假服务，测完即关。
（本机 3000 若真有桥接在跑，也不能拿它做断言对象——那会把用户的通达信画面拖着跳。）
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services import tdx_link


class _Handler(BaseHTTPRequestHandler):
    status = 200
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "success"}).encode())

    def log_message(self, *_args):  # 别把测试请求打进控制台
        pass


@pytest.fixture()
def fake_bridge():
    _Handler.hits = []
    _Handler.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Handler)   # 端口 0 = 让系统给个空闲端口
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_digits_of_strips_suffix():
    assert tdx_link.digits_of("600156.SH") == "600156"
    assert tdx_link.digits_of("300750.SZ") == "300750"
    assert tdx_link.digits_of(" 600156 ") == "600156"
    assert tdx_link.digits_of("") == ""
    assert tdx_link.digits_of(None) == ""


def test_build_url_keeps_six_digits_only():
    url = tdx_link.build_url("600156")
    assert url == "http://127.0.0.1:3000/tdx?code=600156"
    # 末尾斜杠不能拼出 //tdx
    assert tdx_link.build_url("600156", "http://127.0.0.1:3000/") == url


def test_send_hits_bridge_with_six_digits(fake_bridge):
    ok, text = tdx_link.send("600156.SH", fake_bridge, timeout=3)
    assert ok is True
    assert "已联动" in text and "600156" in text
    assert _Handler.hits == ["/tdx?code=600156"], "必须只发一次，且带 6 位代码"


def test_bad_code_is_rejected_without_request(fake_bridge):
    for bad in ("abc", "60015", "6001567", ""):
        ok, text = tdx_link.send(bad, fake_bridge, timeout=3)
        assert ok is False and "不是 6 位代码" in text
    assert _Handler.hits == [], "代码不合法就不该发请求"


def test_http_error_is_reported(fake_bridge):
    _Handler.status = 500
    ok, text = tdx_link.send("600156.SH", fake_bridge, timeout=3)
    assert ok is False and "HTTP 500" in text


def test_no_service_is_reported():
    # 端口 1 在本机不会有 HTTP 服务；文案必须提示「没启动」而不是抛异常
    ok, text = tdx_link.send("600156.SH", "http://127.0.0.1:1", timeout=1)
    assert ok is False
    assert "联动失败" in text


def test_localhost_bypasses_proxy(monkeypatch, fake_bridge):
    """HTTP_PROXY 指向死端口时，本机桥接仍必须直连成功。

    走代理的话，代理会替我们回一个 200，看着「成功」其实请求没到桥接——
    这正是没设置 ProxyHandler({}) 时踩过的坑。
    """
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    ok, text = tdx_link.send("600156.SH", fake_bridge, timeout=3)
    assert ok is True, text
    assert _Handler.hits == ["/tdx?code=600156"], "请求必须真的到达本机桥接"


# ================================================================ 单股精盯页按钮
@pytest.fixture(scope="module")
def qapp():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def _gap(left_widget, right_widget, root) -> int:
    from PySide6.QtCore import QPoint

    right_edge = left_widget.mapTo(root, QPoint(left_widget.width(), 0)).x()
    return right_widget.mapTo(root, QPoint(0, 0)).x() - right_edge


def test_single_view_button_matches_toolbar(qapp, fake_bridge):
    """「通达信联动」要在涨跌配色后面，尺寸/间距和工具栏其它控件对齐。"""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QPushButton

    from app.ui.views import PERIODS, SingleStockView

    view = SingleStockView({"tdx_link": {"base_url": fake_bridge, "timeout_sec": 3}})
    view.resize(1280, 420)
    view.show()
    qapp.processEvents()

    btn, scheme = view.btn_tdx, view.scheme
    last_period = view.period_buttons[PERIODS[-1][0]]

    assert isinstance(btn, QPushButton) and btn.text() == "通达信联动"
    assert btn.width() == scheme.width() == 96, "宽度与「涨跌配色」一致"
    assert btn.height() == view.period_buttons["1d"].height(), "高度与周期按钮一致"
    assert _gap(scheme, btn, view) == _gap(last_period, scheme, view), "与配色控件等距"
    assert not btn.isEnabled(), "没有标的时置灰"


def test_single_view_button_links_current_symbol(qapp, fake_bridge):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from app.ui.views import SingleStockView

    view = SingleStockView({"tdx_link": {"base_url": fake_bridge, "timeout_sec": 3}})
    view.set_symbol("600156.SH")
    assert view.btn_tdx.isEnabled(), "选到股票就该能点"

    messages: list[str] = []
    view.tdx_message.connect(messages.append)
    QTest.mouseClick(view.btn_tdx, Qt.LeftButton)

    deadline = time.time() + 5
    while not messages and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)

    assert _Handler.hits == ["/tdx?code=600156"], "点一次发一次，且带 6 位代码"
    assert messages and "已联动" in messages[0]

    view.set_symbol("")
    assert not view.btn_tdx.isEnabled(), "标的清空后要跟着置灰"


def test_single_view_clear_symbol_disables_button(qapp):
    from app.ui.views import SingleStockView

    view = SingleStockView()
    view.set_symbol("000001.SZ")
    assert view.btn_tdx.isEnabled()
    view.clear_symbol()
    assert not view.btn_tdx.isEnabled(), "空分组时不能把旧代码发出去"
