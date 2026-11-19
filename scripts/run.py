"""启动桌面端。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    executable_dir = Path(sys.executable).resolve().parent
    os.chdir(executable_dir)

    template = Path(sys._MEIPASS) / "config.example.yaml"
    config = executable_dir / "config.yaml"
    if not config.exists():
        config.write_bytes(template.read_bytes())

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import main

if __name__ == "__main__":
    raise SystemExit(main())
