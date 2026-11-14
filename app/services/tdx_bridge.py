"""通达信桥接（模块G）：代码标准化、去重、落库、记录事件。"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

CODE_RE = re.compile(r"^(\d{6})(?:\.(SH|SZ|BJ))?$")
SUPPORTED_PREFIX = {
    "5": "SH", "6": "SH", "9": "SH",
    "0": "SZ", "1": "SZ", "2": "SZ", "3": "SZ",
    "4": "BJ", "8": "BJ",
}


def _infer_market(digits: str) -> str | None:
    return SUPPORTED_PREFIX.get(digits[0])


def normalize_symbol(code: str) -> str | None:
    """6 位代码转标准 symbol。前缀与后缀冲突、或超出支持范围返回 None。"""
    raw = str(code).strip().upper()
    match = CODE_RE.match(raw)
    if not match:
        return None
    digits, suffix = match.group(1), match.group(2)
    inferred = _infer_market(digits)
    if inferred is None:
        return None
    if suffix and suffix != inferred:
        return None
    return f"{digits}.{inferred}"


def parse_payload(payload: dict) -> list[dict]:
    """解析桥接事件，返回 [{symbol, name, group}]。"""
    out: list[dict] = []
    for item in payload.get("symbols", []):
        symbol = normalize_symbol(item.get("code", ""))
        if not symbol:
            continue
        out.append({
            "symbol": symbol,
            "name": item.get("name", ""),
            "group": item.get("group") or payload.get("group") or "盘中异动池",
        })
    return out


def ingest(cur: sqlite3.Cursor, payload: dict, channel: str = "json_watch") -> dict:
    """导入一批股票，返回统计。重复自动忽略。"""
    items = parse_payload(payload)
    now = datetime.now()
    new_count = 0
    for item in items:
        cur.execute(
            "INSERT OR IGNORE INTO watch_groups (name, sort_order) VALUES (?, 999)",
            (item["group"],),
        )
        cur.execute("SELECT id FROM watch_groups WHERE name = ?", (item["group"],))
        row = cur.fetchone()
        if row is None:
            continue
        group_id = row[0]
        cur.execute(
            "INSERT OR IGNORE INTO stock_info (symbol, code, name, market, source) VALUES (?, ?, ?, ?, ?)",
            (item["symbol"], item["symbol"].split(".")[0], item["name"] or "未命名",
             item["symbol"].split(".")[1], "tongdaxin"),
        )
        cur.execute(
            "INSERT OR IGNORE INTO watch_group_stocks (group_id, symbol, sort_order) VALUES (?, ?, 999)",
            (group_id, item["symbol"]),
        )
        if cur.rowcount:
            new_count += 1

    cur.execute(
        """INSERT INTO tdx_events
           (event, channel, group_name, raw_payload, symbol_cnt, new_cnt, event_time)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (payload.get("event", "unknown"), channel, payload.get("group"),
         json.dumps(payload, ensure_ascii=False), len(items), new_count,
         now.isoformat(timespec="seconds")),
    )
    return {"total": len(items), "new": new_count}


def read_csv(path: str | Path, group: str) -> dict:
    """CSV 通道：每行 code[,name]。"""
    symbols = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not parts or not parts[0] or not parts[0][0].isdigit():
            continue
        symbols.append({"code": parts[0], "name": parts[1] if len(parts) > 1 else "", "group": group})
    return {"source": "tongdaxin", "event": "watchlist_update",
            "timestamp": datetime.now().isoformat(timespec="seconds"), "symbols": symbols}
