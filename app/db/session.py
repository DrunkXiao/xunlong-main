"""SQLite 访问层：统一连接、建表、备份。

并发说明：poller 线程、股票池导入线程与 UI 线程会同时写库。
SQLite 在 WAL 模式下多写入线程序列化不当会抛 "attempt to write a readonly database"，
因此这里用一把进程内写锁串行化所有 cursor 使用，并给每条连接设 busy_timeout。
"""
from __future__ import annotations

import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

SCHEMA_FILENAME = "schema.sql"

# 进程内串行化：一个应用只有一个 SQLite 文件，锁粒度按库即可
_WRITE_LOCK = threading.RLock()


# 老库升级：schema.sql 用的是 CREATE TABLE IF NOT EXISTS，新增列不会自动落到已有表上，
# 这里按需 ALTER，保证「老库直接开新版本」也能跑。
_ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "dragon_tiger_records": [
        ("name", "TEXT"),
        ("buy_amount", "REAL"),
        ("sell_amount", "REAL"),
        ("change_pct", "REAL"),
        ("close_price", "REAL"),
        ("turnover", "REAL"),
        ("trade_market", "TEXT"),
        ("fetched_at", "TEXT"),
        ("data_level", "TEXT NOT NULL DEFAULT 'C'"),
        ("quality_flag", "TEXT"),
    ],
}


class Database:
    def __init__(self, db_path: str | Path, schema_path: str | Path | None = None) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if schema_path is None:
            schema_path = Path(__file__).with_name(SCHEMA_FILENAME)
        self.schema_path = Path(schema_path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=20.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 20000")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # 其他连接正在写时切换日志模式会失败；库本身已是 WAL，忽略即可
            pass
        return conn

    @contextmanager
    def cursor(self) -> Iterator[sqlite3.Cursor]:
        with _WRITE_LOCK:
            conn = self.connect()
            try:
                yield conn.cursor()
                conn.commit()
            finally:
                conn.close()

    def init_schema(self) -> int:
        """执行 schema.sql，返回表数量。重复执行为幂等。"""
        if not self.schema_path.exists():
            raise FileNotFoundError(f"缺少建表脚本: {self.schema_path}")
        sql = self.schema_path.read_text(encoding="utf-8")
        with self.cursor() as cur:
            cur.executescript(sql)
            self._migrate(cur)
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return len(cur.fetchall())

    @staticmethod
    def _migrate(cur: sqlite3.Cursor) -> int:
        """给已存在的表补新增列。返回补了几列。"""
        added = 0
        for table, columns in _ADDED_COLUMNS.items():
            cur.execute(f"PRAGMA table_info({table})")
            have = {row[1] for row in cur.fetchall()}
            if not have:
                continue
            for name, ddl in columns:
                if name not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    added += 1
        return added

    def seed_default_groups(self, names: list[str] | None = None) -> None:
        """写入默认分组（模块H），已存在则跳过。"""
        groups = names or [
            "龙头核心池", "市场最高板", "连板晋级池", "首板观察池", "二波启动池",
            "强势趋势池", "主线题材池", "龙虎榜观察池", "盘中异动池", "待复盘股票池",
        ]
        with self.cursor() as cur:
            for idx, name in enumerate(groups):
                cur.execute(
                    "INSERT OR IGNORE INTO watch_groups (name, sort_order, is_default) VALUES (?, ?, 1)",
                    (name, idx),
                )

    def backup(self, keep: int = 5) -> Path:
        """本地数据库备份（铁律：必须支持备份恢复）。"""
        backup_dir = self.path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = backup_dir / f"{self.path.stem}_{stamp}.db"
        shutil.copy2(self.path, target)
        backups = sorted(backup_dir.glob(f"{self.path.stem}_*.db"))
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
        return target

    def table_names(self) -> list[str]:
        with self.cursor() as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            return [row["name"] for row in cur.fetchall()]
