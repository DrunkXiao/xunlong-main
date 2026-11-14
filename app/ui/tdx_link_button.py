"""「通达信联动」按钮：把当前代码推给本机桥接，全程不阻塞界面。

两个二级页面（强势回调 / 涨停四阶）共用这一个控件，口径保持一致：
选中标的才可点、点一次发一次、失败只写日志不弹窗。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QPushButton

from app.services import tdx_link

BUSY_TEXT = "联动中…"
DONE_TEXT = "已联动"


class TdxLinkButton(QPushButton):
    """选中标的后点它：后台线程发一次 GET，结果回主线程刷新按钮与日志。"""

    message = Signal(str)          # 需要写进右下角日志的文案
    _finished = Signal(bool, str)  # 工作线程 → 主线程

    def __init__(self, config: dict | None = None, parent=None) -> None:
        super().__init__("通达信联动", parent)
        link = (config or {}).get("tdx_link") or {}
        self._base = link.get("base_url") or tdx_link.DEFAULT_BASE
        self._timeout = float(link.get("timeout_sec") or tdx_link.DEFAULT_TIMEOUT)
        self._symbol = ""
        self._busy = False
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(lambda: self.setText("通达信联动"))

        self.setEnabled(False)
        self.setToolTip(
            f"把当前代码发给本机通达信桥接：\nGET {tdx_link.endpoint(self._base)}?code=<6位代码>\n"
            "桥接服务未启动时只写一条失败日志，不影响扫描与行情。")
        self.clicked.connect(self._fire)
        self._finished.connect(self._on_finished)

    # ---------------------------------------------------------------- 外部接口
    def set_symbol(self, symbol: str | None) -> None:
        """当前选中标的；传 None 表示没有选中，按钮置灰。"""
        self._symbol = (symbol or "").strip()
        self.setEnabled(bool(self._symbol) and not self._busy)

    # ---------------------------------------------------------------- 内部
    def _fire(self) -> None:
        if self._busy or not self._symbol:
            return
        code = tdx_link.digits_of(self._symbol)
        self._busy = True
        self._reset_timer.stop()
        self.setEnabled(False)
        self.setText(BUSY_TEXT)
        threading.Thread(target=self._run, args=(code,), daemon=True).start()

    def _run(self, code: str) -> None:
        ok, text = tdx_link.send(code, self._base, self._timeout)
        self._finished.emit(ok, text)

    def _on_finished(self, ok: bool, text: str) -> None:
        self._busy = False
        self.setText(DONE_TEXT if ok else "通达信联动")
        self.setEnabled(bool(self._symbol))
        if ok:
            self._reset_timer.start(1500)
        self.message.emit(text)
