"""测试公共设置：强制 Qt 无界面运行，避免弹出窗口。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_stockdb_sdk():
    """每个用例前清掉 stock_sdk 的进程级状态。

    `sdk_bootstrap` 把 `rd` 和 `sys.modules["stock_sdk"]` 一起缓存，并且会把
    SDK 目录插进 `sys.path`。只要有任何一个用例真的 import 过本机的 SDK，
    后面那些「用一个临时目录写假 stock_sdk.py」的用例就会拿到缓存里的真模块，
    断言 `rd.tag == 'fake'` 直接 AttributeError —— 单独跑通过、全量跑失败。
    这里在用例开始前把模块缓存与状态还原干净，不改动 sys.path（改它会影响
    其它用例对真实路径的解析）。
    """
    from app.adapters import sdk_bootstrap

    saved_state = dict(sdk_bootstrap._STATE)
    saved_modules = {name: mod for name, mod in sys.modules.items()
                     if name in ("stock_sdk", "stockdb")}
    for name in ("stock_sdk", "stockdb"):
        sys.modules.pop(name, None)
    sdk_bootstrap._STATE.update(rd=None, dir="", error=None, tried=[])
    try:
        yield
    finally:
        for name in ("stock_sdk", "stockdb"):
            sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sdk_bootstrap._STATE.clear()
        sdk_bootstrap._STATE.update(saved_state)

