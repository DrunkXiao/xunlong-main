"""初始化数据库：建表 + 默认分组 + 备份。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import load_config, sqlite_path_from_url
from app.db.session import Database


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    config = load_config(root / "config.yaml")
    db_path = sqlite_path_from_url(config["database"]["url"])
    if not db_path.is_absolute():
        db_path = root / db_path
    database = Database(db_path, schema_path=root / "app" / "db" / "schema.sql")
    table_count = database.init_schema()
    database.seed_default_groups()
    print(f"数据库: {db_path}")
    print(f"建表数量: {table_count}")
    print(f"表清单: {', '.join(database.table_names())}")
    print("初始化完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
