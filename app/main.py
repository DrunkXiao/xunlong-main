"""桌面端启动入口。默认只开窗口；加 --api 才启动本地 HTTP 服务。"""
from __future__ import annotations

import argparse
import sys
import threading

from app.config import load_config


def _serve_api(host: str, port: int) -> None:
    try:
        import uvicorn
    except ImportError:
        print("未安装 uvicorn/fastapi，本地接口服务不启动（桌面端仍可使用）")
        return
    uvicorn.run("app.api.app:app", host=host, port=port, log_level="warning")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="寻龙盯盘 Dragon Hunter")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--api", action="store_true", help="同时启动本地 HTTP 接口")
    args, qt_args = parser.parse_known_args(argv)

    config = load_config(args.config)
    if args.api:
        port = int(config.get("tdx_bridge", {}).get("http_port", 8611))
        threading.Thread(target=_serve_api, args=("127.0.0.1", port), daemon=True).start()

    # 必须在 import PySide6 之前预加载 stock_sdk：Qt 的 shiboken 导入钩子会
    # 对 stockdb.pyd 做 inspect.unwrap()，抛 "wrapper loop"，之后再导就一律失败。
    # 失败不阻断开窗，只记在日志里，分时页会显示可操作提示。
    from app.adapters import sdk_bootstrap

    sdk_ok, sdk_note = sdk_bootstrap.preload(args.config)
    if not sdk_ok:
        print(f"[分时] stock_sdk 未就绪：{sdk_note}", file=sys.stderr)

    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("缺少 PySide6，请先执行: pip install -r requirements.txt")
        return 1
    from app.ui.main_window import MainWindow

    qt_app = QApplication([sys.argv[0], *qt_args])
    qt_app.setApplicationName("寻龙盯盘 Dragon Hunter")
    window = MainWindow(args.config)
    window.show()
    return qt_app.exec()


if __name__ == "__main__":
    sys.exit(main())
