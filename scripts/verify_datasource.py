"""数据源自检：接真实行情并把状态写库。

用法:
    python scripts/verify_datasource.py [symbol]

退出码 0 表示数据源可用；1 表示不可用（错误信息会打印原因，不会伪造数据）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters import build_adapter
from app.adapters.base import DataSourceError
from app.config import load_config, sqlite_path_from_url
from app.db.session import Database


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "600000.SH"
    config = load_config(ROOT / "config.yaml")
    adapter = build_adapter(config)
    caps = adapter.capabilities
    print(f"数据源: {config['datasource']['name']} | 等级 {caps.data_level}")
    print(f"能力: {caps.summary()}")
    print(f"说明: {caps.note}")

    health = adapter.health_check() if hasattr(adapter, "health_check") else {"connected": True}
    print(f"健康: {health}")

    try:
        quote = adapter.get_quote(symbol)
    except DataSourceError as exc:
        print(f"取行情失败 [{exc.kind}] {exc.message}")
        return 1
    pct = f"{quote.change_pct:+.2f}%" if quote.change_pct is not None else "N/A"
    amount = f"{(quote.amount or 0) / 1e8:.2f}亿" if quote.amount else "N/A"
    print(f"行情: {quote.name} {quote.symbol} {quote.price} {pct} {amount} "
          f"换手{quote.turnover_rate} 量比{quote.volume_ratio} 时间源={quote.time_source}")

    try:
        bars = adapter.get_kline(symbol, "1d", limit=5)
        print(f"日K: {len(bars)} 根, 最后 {bars[-1].bar_time:%Y-%m-%d} close={bars[-1].close}")
    except DataSourceError as exc:
        print(f"日K不可用 [{exc.kind}] {exc.message}")

    for period in ("5m", "1m"):
        try:
            adapter.get_kline(symbol, period, limit=5)
            print(f"{period}: 意外可用，请复核能力矩阵")
        except DataSourceError as exc:
            print(f"{period}: 按预期不可用 [{exc.kind}]")

    # 落库：行情缓存 + 数据源状态，证明链路通到 SQLite
    db_path = sqlite_path_from_url(config["database"]["url"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    database = Database(db_path, schema_path=ROOT / "app" / "db" / "schema.sql")
    if not db_path.exists():
        database.init_schema()
    now = datetime.now().isoformat(timespec="seconds")
    with database.cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO stock_info (symbol, code, name, market, source) VALUES (?, ?, ?, ?, ?)",
            (quote.symbol, quote.symbol.split(".")[0], quote.name,
             quote.symbol.split(".")[1], quote.source),
        )
        cur.execute(
            """INSERT INTO quotes_cache
               (symbol, price, prev_close, open, high, low, volume, amount,
                change_pct, turnover_rate, volume_ratio, limit_up_price, limit_down_price,
                data_level, source, quote_time, received_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 price=excluded.price, amount=excluded.amount,
                 data_level=excluded.data_level, quote_time=excluded.quote_time,
                 received_at=excluded.received_at, updated_at=excluded.updated_at""",
            (quote.symbol, quote.price, quote.prev_close, quote.open, quote.high, quote.low,
             quote.volume, quote.amount, quote.change_pct, quote.turnover_rate, quote.volume_ratio,
             quote.limit_up_price, quote.limit_down_price, quote.data_level, quote.source,
             quote.quote_time.isoformat(timespec="seconds") if quote.quote_time else None,
             quote.received_at.isoformat(timespec="seconds") if quote.received_at else None,
             now),
        )
        cur.execute(
            """INSERT INTO data_source_status (source, data_level, connected, latency_ms, data_timestamp, checked_at)
               VALUES (?, ?, 1, ?, ?, ?)
               ON CONFLICT(source, data_level) DO UPDATE SET
                 connected=1, latency_ms=excluded.latency_ms,
                 data_timestamp=excluded.data_timestamp, checked_at=excluded.checked_at""",
            (config["datasource"]["name"], caps.data_level,
             health.get("latency_ms"), now, now),
        )
    with database.cursor() as cur:
        cur.execute("SELECT symbol, price, change_pct, data_level, updated_at FROM quotes_cache")
        rows = cur.fetchall()
    print(f"落库: quotes_cache {len(rows)} 行 -> {[dict(r) for r in rows]}")

    if hasattr(adapter, "disconnect"):
        adapter.disconnect()
    print("数据源自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
