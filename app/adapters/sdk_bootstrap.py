"""stock_sdk 的进程级预加载 —— 负责找到 SDK、导入一次、缓存起来。

## 为什么需要这个模块

PySide6 的 shiboken 把 `builtins.__import__` 换成了 C 函数 `__feature_import__`：
每有新模块被导入，它都会调用 `inspect.getsource()` / `inspect.unwrap()` 判断
「这个模块是否用到 PySide」。对 C 扩展 `stockdb.pyd`（单阶段初始化模块），
`inspect.unwrap()` 会命中包装循环：

    ValueError: wrapper loop when unwrapping <module 'stockdb' from '...stockdb.pyd'>

于是 **只要 Qt 先加载，之后任何 `import stock_sdk` 都必然失败**，而且
`sys.modules` 里会留下一个半初始化的 `stockdb`，后续重试还是同一个错——
分时页就一直空白，报错还说「主源同样失败」，完全看不出真正原因。

## 两道保险

1. **预加载**：`app/main.py` 在 `import PySide6` 之前调用 `preload()`，
   钩子还没装上，导入最干净、最快。打包后的 exe 里 PyInstaller 的 PySide6
   运行时钩子会抢先加载 Qt，预加载也来不及——所以还需要第 2 条。
2. **绕过钩子**：`_import_rd()` 在导入瞬间把 `builtins.__import__` 换回
   `importlib.__import__`（纯 Python 实现，语义一致），导完立刻换回去。
   实测 Qt 已加载时用这条也能成功导入并取到 242 根分钟线。

导入成功后 `rd` 进程级缓存，后续一律从 `sys.modules` 取，不再 trigger
`stockdb` 的导入（已缓存的模块再 import 一样会抛 wrapper loop）。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from app.adapters.base import DataSourceError

# 没配 sdk_dir 时的兜底搜索位置
DEFAULT_SDK_DIRS = (
    r"G:/stock/stockdb/pybao",
    r"D:/stockdb/pybao",
    r"C:/stockdb/pybao",
    r"D:/new_tdx64/stockdb/pybao",
)

_LOCK = threading.RLock()
_STATE: dict[str, object] = {"rd": None, "dir": "", "error": None, "tried": []}


def candidates(sdk_dir: str | Path | None = None) -> list[str]:
    """按优先级列出候选目录：显式配置 > 环境变量 > 默认位置。"""
    out: list[str] = []
    for item in (sdk_dir, os.environ.get("STOCKDB_SDK_DIR", "")):
        text = str(item or "").strip()
        if text:
            out.append(text)
    out.extend(DEFAULT_SDK_DIRS)
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen


def _looks_like_qt_conflict(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return "wrapper loop" in text or "shiboken" in text.lower()


def _import_rd(path: str):
    """在「绕过 shiboken 导入钩子」的环境里执行 `from stock_sdk import rd`。

    Shiboken 把 `builtins.__import__` 换成了 C 函数 `__feature_import__`，
    每导一个模块都要 `inspect.unwrap()` 一次，撞上 `stockdb.pyd` 就抛 wrapper loop。
    这里临时换回 `importlib.__import__`（纯 Python 实现，语义一致）把它做完，
    再原样换回去——不能永久替换：全局换成 Python 版会让整个程序每次 import 都变慢。

    这样即使 Qt 已经先加载（打包后的 exe 就是：PyInstaller 的 PySide6 运行时钩子
    比我们的入口脚本更早），SDK 依然能导入。
    """
    import builtins
    import importlib

    saved = builtins.__import__
    swapped = getattr(saved, "__name__", "") != "__import__"
    if str(path) not in sys.path:
        sys.path.insert(0, path)
    if swapped:
        builtins.__import__ = importlib.__import__
    try:
        from stock_sdk import rd          # noqa: PLC0415（必须延迟导入）
        return rd
    except Exception:
        # 失败时可能留下半初始化的 stockdb，不清掉的话下一次重试仍是同一个错
        sys.modules.pop("stockdb", None)
        sys.modules.pop("stock_sdk", None)
        raise
    finally:
        if swapped:
            builtins.__import__ = saved


def load(sdk_dir: str | Path | None = None):
    """返回 `stock_sdk.rd`；找不到或导入失败抛 `DataSourceError`。

    成功结果进程级缓存，重复调用零成本。失败不缓存（用户可能刚改完配置）。
    """
    with _LOCK:
        cached = _STATE.get("rd")
        if cached is not None:
            return cached

        tried: list[str] = []
        for folder in candidates(sdk_dir):
            path = Path(folder)
            if not (path / "stock_sdk.py").is_file():
                tried.append(f"{folder}（无 stock_sdk.py）")
                continue
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                rd = _import_rd(str(path))
            except Exception as exc:              # pyd 位数/版本不匹配、Qt 钩子冲突等
                if _looks_like_qt_conflict(exc):
                    raise DataSourceError(
                        kind="qt_conflict",
                        message=(
                            "stockdb.pyd 与 PySide6 的导入钩子冲突（wrapper loop），"
                            "且绕过钩子后仍然失败。请确认 stockdb.pyd 的位数与 Python 版本"
                            f"与当前解释器一致（{sys.version.split()[0]}，"
                            f"{'64' if sys.maxsize > 2**32 else '32'} 位），"
                            "或改用对应的 3.14t+ / 标准版本文件。"
                        ),
                        retryable=False,
                    ) from exc
                tried.append(f"{folder}（导入失败：{type(exc).__name__}: {exc}）")
                continue
            _STATE.update(rd=rd, dir=str(path), error=None, tried=tried)
            return rd

        # 已经在 sys.modules 里（预加载过）就直接取，绝不再触发一次 import
        module = sys.modules.get("stock_sdk")
        if module is not None:
            rd = getattr(module, "rd", None)
            if rd is not None:
                _STATE.update(rd=rd, dir=str(_STATE.get("dir") or "sys.modules"), error=None)
                return rd

        _STATE["tried"] = tried
        raise DataSourceError(
            kind="not_installed",
            message=(
                "找不到 free-stockdb 的 Python SDK（stock_sdk.py + stockdb.pyd）。"
                f"已尝试：{'；'.join(tried) or '（未配置 sdk_dir）'}。"
                "请把引擎安装目录下的 pybao 文件夹路径填到 "
                "config.yaml → intraday.free_stockdb.sdk_dir"
            ),
            retryable=False,
        )


def loaded_dir() -> str:
    """实际加载成功的目录（界面/日志用）。"""
    return str(_STATE.get("dir") or "")


def preload(config_path: str | Path | None = None) -> tuple[bool, str]:
    """入口处调用：先尝试从配置文件读 sdk_dir，再按候选目录加载。

    永不抛异常——SDK 装没装不该决定主程序能不能开窗；
    失败原因留在返回值和 `_STATE["error"]` 里，由分时页显示成可操作提示。
    """
    sdk_dir = ""
    for path in ([config_path] if config_path else []) + ["config.yaml"]:
        if not path:
            continue
        file = Path(path)
        if not file.is_file():
            continue
        try:
            from app.config import load_config      # 不 import Qt，可以安全用

            sdk_dir = str((((load_config(file).get("intraday") or {})
                            .get("free_stockdb") or {}).get("sdk_dir") or "")).strip()
            break
        except Exception:
            continue
    try:
        rd = load(sdk_dir)
    except DataSourceError as exc:
        _STATE["error"] = exc.message
        return False, exc.message
    return True, f"stock_sdk 已预加载：{loaded_dir()}（rd={type(rd).__name__}）"
