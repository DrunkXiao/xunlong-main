"""持久化仓储：分组、布局、设置、行情缓存、K线、预警。

铁律 7：分组顺序、布局、预警条件、窗口位置必须落 SQLite，重启原样恢复。
所有方法都自建连接（WAL 模式），因此可从工作线程直接调用。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Sequence

from app.db.models import AlertEvent, Bar, Quote
from app.db.session import Database


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Repo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ================================================================ 基础信息
    def upsert_stocks(self, rows: Iterable[dict]) -> int:
        """写入/更新代码表。返回实际写入行数。

        只更新 name / market / board / source，不覆盖 status（人工标记的退市/停牌要保留）。
        """
        payload = []
        for row in rows:
            symbol = str(row.get("symbol", "")).strip().upper()
            if not symbol or "." not in symbol:
                continue
            code, market = symbol.split(".", 1)
            payload.append((
                symbol, code, market, str(row.get("name") or code),
                row.get("board"), row.get("source") or "tqcenter",
            ))
        if not payload:
            return 0
        with self.db.cursor() as cur:
            cur.executemany(
                """INSERT INTO stock_info (symbol, code, market, name, board, source)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(symbol) DO UPDATE SET
                       name = excluded.name,
                       board = excluded.board,
                       source = excluded.source,
                       updated_at = datetime('now', 'localtime')""",
                payload,
            )
        return len(payload)

    def stock_count(self) -> int:
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM stock_info")
            return int(cur.fetchone()["n"])

    def get_stock(self, symbol: str) -> dict | None:
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM stock_info WHERE symbol = ?", (symbol,))
            row = cur.fetchone()
            return dict(row) if row else None

    def stock_names(self, symbols: Iterable[str]) -> dict[str, str]:
        symbols = list(symbols)
        if not symbols:
            return {}
        marks = ",".join("?" * len(symbols))
        with self.db.cursor() as cur:
            cur.execute(f"SELECT symbol, name FROM stock_info WHERE symbol IN ({marks})", symbols)
            return {row["symbol"]: row["name"] for row in cur.fetchall()}

    def all_stocks(self) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute("SELECT symbol, code, name, market, board FROM stock_info ORDER BY symbol")
            return [dict(row) for row in cur.fetchall()]

    def search(self, query: str, limit: int = 40) -> list[dict]:
        """按代码前缀 / 名称包含检索。拼音由 UniverseService 在内存层补充。"""
        q = (query or "").strip().upper()
        if not q:
            return []
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT symbol, code, name, market, board FROM stock_info
                   WHERE code LIKE ? OR symbol LIKE ? OR name LIKE ?
                   ORDER BY (code = ?) DESC, (code LIKE ?) DESC, code
                   LIMIT ?""",
                (f"{q}%", f"{q}%", f"%{q}%", q, f"{q}%", limit),
            )
            return [dict(row) for row in cur.fetchall()]

    def symbols_by_prefix(self, prefix: str, limit: int = 40) -> list[str]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM stock_info WHERE symbol LIKE ? ORDER BY symbol LIMIT ?",
                (f"{prefix.upper()}%", limit),
            )
            return [row["symbol"] for row in cur.fetchall()]

    # ================================================================ 分组
    def groups(self) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT g.id, g.name, g.sort_order, g.is_default,
                          (SELECT COUNT(*) FROM watch_group_stocks s WHERE s.group_id = g.id) AS count
                   FROM watch_groups g
                   ORDER BY g.sort_order, g.id"""
            )
            return [dict(row) for row in cur.fetchall()]

    def create_group(self, name: str) -> int:
        name = name.strip()
        if not name:
            raise ValueError("分组名不能为空")
        with self.db.cursor() as cur:
            cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM watch_groups")
            order = int(cur.fetchone()["n"])
            cur.execute(
                "INSERT INTO watch_groups (name, sort_order, is_default) VALUES (?, ?, 0)",
                (name, order),
            )
            return int(cur.lastrowid)

    def rename_group(self, group_id: int, new_name: str) -> None:
        new_name = new_name.strip()
        if not new_name:
            raise ValueError("分组名不能为空")
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE watch_groups SET name = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (new_name, group_id),
            )

    def delete_group(self, group_id: int) -> None:
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM watch_groups WHERE id = ?", (group_id,))
            cur.execute("DELETE FROM user_layouts WHERE scope = 'group' AND ref_id = ?", (str(group_id),))

    def reorder_groups(self, ordered_ids: list[int]) -> None:
        with self.db.cursor() as cur:
            for idx, gid in enumerate(ordered_ids):
                cur.execute(
                    "UPDATE watch_groups SET sort_order = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                    (idx, gid),
                )

    def copy_group(self, group_id: int, new_name: str) -> int:
        new_id = self.create_group(new_name)
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO watch_group_stocks (group_id, symbol, sort_order, note)
                   SELECT ?, symbol, sort_order, note FROM watch_group_stocks
                   WHERE group_id = ? ORDER BY sort_order, id""",
                (new_id, group_id),
            )
        return new_id

    # ================================================================ 组内股票
    def group_symbols(self, group_id: int) -> list[str]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT symbol FROM watch_group_stocks WHERE group_id = ? ORDER BY sort_order, id",
                (group_id,),
            )
            return [row["symbol"] for row in cur.fetchall()]

    def add_symbols(self, group_id: int, symbols: Iterable[str]) -> int:
        """加入分组，已存在则跳过（自动去重）。返回新增数量。"""
        unique: list[str] = []
        seen = set()
        for sym in symbols:
            sym = str(sym).strip().upper()
            if sym and sym not in seen:
                seen.add(sym)
                unique.append(sym)
        if not unique:
            return 0
        added = 0
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM watch_group_stocks WHERE group_id = ?",
                (group_id,),
            )
            order = int(cur.fetchone()["n"])
            for sym in unique:
                cur.execute(
                    "SELECT 1 FROM stock_info WHERE symbol = ?", (sym,)
                )
                if cur.fetchone() is None:
                    continue  # 未在代码表中的代码不入组，避免 FK 失败与幽灵行
                cur.execute(
                    """INSERT OR IGNORE INTO watch_group_stocks (group_id, symbol, sort_order)
                       VALUES (?, ?, ?)""",
                    (group_id, sym, order),
                )
                if cur.rowcount:
                    added += 1
                    order += 1
        return added

    def remove_symbols(self, group_id: int, symbols: Iterable[str]) -> int:
        symbols = list(symbols)
        if not symbols:
            return 0
        marks = ",".join("?" * len(symbols))
        with self.db.cursor() as cur:
            cur.execute(
                f"DELETE FROM watch_group_stocks WHERE group_id = ? AND symbol IN ({marks})",
                [group_id, *symbols],
            )
            return cur.rowcount

    def set_group_symbols(self, group_id: int, ordered_symbols: list[str]) -> None:
        """整体重写顺序（拖拽排序后调用）。"""
        with self.db.cursor() as cur:
            for idx, sym in enumerate(ordered_symbols):
                cur.execute(
                    """UPDATE watch_group_stocks SET sort_order = ?, updated_at = datetime('now','localtime')
                       WHERE group_id = ? AND symbol = ?""",
                    (idx, group_id, sym),
                )

    def move_symbol(self, group_id: int, symbol: str, to_index: int) -> list[str]:
        current = self.group_symbols(group_id)
        if symbol not in current:
            return current
        current.remove(symbol)
        to_index = max(0, min(to_index, len(current)))
        current.insert(to_index, symbol)
        self.set_group_symbols(group_id, current)
        return current

    def groups_containing(self, symbol: str) -> list[str]:
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT g.name FROM watch_groups g
                   JOIN watch_group_stocks s ON s.group_id = g.id
                   WHERE s.symbol = ? ORDER BY g.sort_order""",
                (symbol,),
            )
            return [row["name"] for row in cur.fetchall()]

    # ================================================================ 设置与布局
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self.db.cursor() as cur:
            cur.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, datetime('now','localtime'))
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                       updated_at = datetime('now','localtime')""",
                (key, str(value)),
            )

    def get_layout(self, scope: str, ref_id: str = "") -> dict:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT payload FROM user_layouts WHERE scope = ? AND ref_id = ?",
                (scope, ref_id),
            )
            row = cur.fetchone()
        if not row:
            return {}
        try:
            data = json.loads(row["payload"])
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def save_layout(self, scope: str, ref_id: str, payload: dict) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO user_layouts (scope, ref_id, payload, updated_at)
                   VALUES (?, ?, ?, datetime('now','localtime'))
                   ON CONFLICT(scope, ref_id) DO UPDATE SET payload = excluded.payload,
                       updated_at = datetime('now','localtime')""",
                (scope, ref_id, json.dumps(payload, ensure_ascii=False)),
            )

    # ================================================================ 行情缓存
    def upsert_quotes(self, quotes: Iterable[Quote]) -> int:
        rows = []
        for q in quotes:
            if q.price is None:
                continue
            rows.append((
                q.symbol, q.price, q.prev_close, q.open, q.high, q.low,
                (q.price - q.prev_close) if (q.price is not None and q.prev_close) else None,
                q.change_pct, q.volume, q.amount, q.turnover_rate, q.volume_ratio,
                q.limit_up_price, q.limit_down_price, q.data_level, q.source,
                q.quote_time.isoformat(timespec="seconds") if q.quote_time else None,
                q.received_at.isoformat(timespec="seconds") if q.received_at else None,
                q.latency_ms,
            ))
        if not rows:
            return 0
        with self.db.cursor() as cur:
            cur.executemany(
                """INSERT INTO quotes_cache
                   (symbol, price, prev_close, open, high, low, change_amount, change_pct,
                    volume, amount, turnover_rate, volume_ratio, limit_up_price, limit_down_price,
                    data_level, source, quote_time, received_at, latency_ms, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))
                   ON CONFLICT(symbol) DO UPDATE SET
                       price=excluded.price, prev_close=excluded.prev_close, open=excluded.open,
                       high=excluded.high, low=excluded.low, change_amount=excluded.change_amount,
                       change_pct=excluded.change_pct, volume=excluded.volume, amount=excluded.amount,
                       turnover_rate=excluded.turnover_rate, volume_ratio=excluded.volume_ratio,
                       limit_up_price=excluded.limit_up_price, limit_down_price=excluded.limit_down_price,
                       data_level=excluded.data_level, source=excluded.source,
                       quote_time=excluded.quote_time, received_at=excluded.received_at,
                       latency_ms=excluded.latency_ms, updated_at=datetime('now','localtime')""",
                rows,
            )
        return len(rows)

    def cached_quotes(self, symbols: Iterable[str]) -> dict[str, dict]:
        symbols = list(symbols)
        if not symbols:
            return {}
        marks = ",".join("?" * len(symbols))
        with self.db.cursor() as cur:
            cur.execute(f"SELECT * FROM quotes_cache WHERE symbol IN ({marks})", symbols)
            return {row["symbol"]: dict(row) for row in cur.fetchall()}

    # ================================================================ K 线
    def upsert_bars(self, bars: Iterable[Bar]) -> int:
        rows = [
            (b.symbol, b.period, b.bar_time.isoformat(timespec="seconds"),
             b.open, b.high, b.low, b.close, b.volume, b.amount,
             b.data_level, b.source, getattr(b, "quality_flag", "exchange"))
            for b in bars
        ]
        if not rows:
            return 0
        with self.db.cursor() as cur:
            cur.executemany(
                """INSERT INTO kline_history
                   (symbol, period, bar_time, open, high, low, close, volume, amount,
                    data_level, source, quality_flag)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol, period, bar_time) DO UPDATE SET
                       open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
                       volume=excluded.volume, amount=excluded.amount,
                       data_level=excluded.data_level, source=excluded.source,
                       quality_flag=excluded.quality_flag""",
                rows,
            )
        return len(rows)

    def load_bars(self, symbol: str, period: str, limit: int = 400,
                  quality_contains: str | Sequence[str] | None = None) -> list[Bar]:
        """按时间正序取 K 线。

        `quality_contains` 用于只看某类来源（如 `em_minute` 只取东财分钟线）：
        1m 表里分钟线与快照采样混存，按来源过滤要在 SQL 里做，否则 limit 会被
        另一类行挤占，看起来「分钟线数据没了」。

        分时来源可能不止一个（东财 / free-stockdb），传字符串序列时按 OR 匹配，
        这样「所有真分钟线」一个条件就能筛出来。
        """
        sql = """SELECT * FROM kline_history WHERE symbol = ? AND period = ?"""
        params: list = [symbol, period]
        marks = [quality_contains] if isinstance(quality_contains, str) else list(quality_contains or [])
        marks = [m for m in marks if m]
        if marks:
            sql += " AND (" + " OR ".join("quality_flag LIKE ?" for _ in marks) + ")"
            params.extend(f"%{mark}%" for mark in marks)
        sql += " ORDER BY bar_time DESC LIMIT ?"
        params.append(limit)
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        bars = []
        for row in reversed(rows):
            bars.append(Bar(
                symbol=row["symbol"], period=row["period"],
                bar_time=datetime.fromisoformat(row["bar_time"]),
                open=row["open"], high=row["high"], low=row["low"], close=row["close"],
                volume=row["volume"] or 0, amount=row["amount"] or 0.0,
                data_level=row["data_level"], source=row["source"] or "",
                quality_flag=row["quality_flag"] or "exchange",
            ))
        return bars

    # ================================================================ 预警规则
    def rules(self, enabled_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM alert_rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        with self.db.cursor() as cur:
            cur.execute(sql)
            out = []
            for row in cur.fetchall():
                item = dict(row)
                try:
                    item["params"] = json.loads(item.get("params") or "{}")
                except json.JSONDecodeError:
                    item["params"] = {}
                try:
                    item["symbols"] = json.loads(item.get("symbols") or "[]")
                except (json.JSONDecodeError, TypeError):
                    item["symbols"] = []
                out.append(item)
            return out

    def add_rule(self, *, name: str, category: str, rule_type: str, level: str,
                 params: dict, symbols: list[str] | None = None, group_id: int | None = None,
                 cooldown_sec: int = 300, notify: list[str] | None = None,
                 enabled: int = 1) -> int:
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO alert_rules
                   (name, category, rule_type, scope, symbols, group_id, params, level,
                    cooldown_sec, notify, enabled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (name, category, rule_type,
                 "group" if group_id else "symbol",
                 json.dumps(symbols or [], ensure_ascii=False), group_id,
                 json.dumps(params, ensure_ascii=False), level, cooldown_sec,
                 json.dumps(notify or ["log"], ensure_ascii=False), enabled),
            )
            return int(cur.lastrowid)

    def set_rule_enabled(self, rule_id: int, enabled: bool) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE alert_rules SET enabled = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                (1 if enabled else 0, rule_id),
            )

    def delete_rule(self, rule_id: int) -> None:
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))

    # ================================================================ 预警事件
    def insert_alert(self, event: AlertEvent, rule_id: int | None = None) -> int:
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO alert_events
                   (rule_id, symbol, name, event_type, trigger_price, trigger_time, condition,
                    volume, amount, data_level, source, level, handled)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rule_id, event.symbol, event.name, event.event_type, event.trigger_price,
                 event.trigger_time.isoformat(timespec="seconds"), event.condition,
                 event.volume, event.amount, event.data_level, event.source,
                 event.level, event.handled),
            )
            return int(cur.lastrowid)

    def recent_alerts(self, limit: int = 200) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT * FROM alert_events ORDER BY trigger_time DESC, id DESC LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]

    def count_alerts(self) -> int:
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM alert_events")
            return int(cur.fetchone()["n"])

    # ================================================================ 区间大单
    def insert_large_orders(self, orders: Iterable) -> int:
        """写入区间大单事件。amount 是区间累计额，不是单笔成交额。

        幂等：同 (symbol, event_time, tier, amount) 重复写入被忽略。
        """
        rows = []
        for order in orders:
            rows.append((
                order.symbol, order.tier, order.direction, order.price, order.volume,
                order.amount, order.threshold,
                order.time.isoformat(timespec="seconds"),
                order.price_position, order.tag, order.data_level, order.source,
            ))
        if not rows:
            return 0
        with self.db.cursor() as cur:
            cur.executemany(
                """INSERT OR IGNORE INTO large_order_events
                   (symbol, tier, direction, price, volume, amount, threshold, event_time,
                    price_position, tag, data_level, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            return cur.rowcount

    def recent_large_orders(self, symbol: str | None = None, limit: int = 200) -> list[dict]:
        sql = "SELECT * FROM large_order_events"
        params: list = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(symbol)
        sql += " ORDER BY event_time DESC, id DESC LIMIT ?"
        params.append(limit)
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def large_orders_by_date(self, symbol: str, days: int = 120) -> dict[str, dict]:
        """按交易日聚合，供 K 线画事件标记。返回 {date: {count, max_tier, buy, sell}}。"""
        tier_rank = {"normal": 1, "large": 2, "super": 3, "continuous": 4}
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT substr(event_time, 1, 10) AS day, tier, direction, amount
                   FROM large_order_events
                   WHERE symbol = ? AND event_time >= date('now', 'localtime', ?)
                   ORDER BY event_time""",
                (symbol, f"-{int(days)} days"),
            )
            rows = cur.fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            item = out.setdefault(row["day"], {"count": 0, "max_tier": None,
                                               "buy": 0.0, "sell": 0.0})
            item["count"] += 1
            if tier_rank.get(row["tier"], 0) >= tier_rank.get(item["max_tier"], 0):
                item["max_tier"] = row["tier"]
            side = row["direction"] if row["direction"] in ("buy", "sell") else None
            if side:
                item[side] += float(row["amount"] or 0.0)
        return out

    def count_large_orders(self) -> int:
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM large_order_events")
            return int(cur.fetchone()["n"])

    # ================================================================ 涨停四阶段股票池
    def upsert_limitup_pool(self, records: Iterable[dict]) -> int:
        """写入涨停四阶段股票池（幂等键 symbol）。状态机跨日推进，必须落库。

        `first_board_*` 只在出现新首板时更新，其余字段每次覆盖：
        后者是"最新行情"，前者是"这一轮的基准"，混在一起会把承接判定带偏。
        """
        payload = []
        for rec in records:
            symbol = str(rec.get("symbol") or "").strip().upper()
            if not symbol or "." not in symbol:
                continue
            payload.append((
                symbol, rec.get("code") or symbol.split(".")[0], rec.get("name"),
                rec.get("market"), rec.get("board"), rec.get("sector"),
                rec.get("sector_tcode"), rec.get("state") or "S1", rec.get("stage"),
                rec.get("first_board_date"), rec.get("first_board_close"),
                rec.get("first_board_volume"), rec.get("first_board_amount"),
                rec.get("first_board_turnover"), rec.get("first_board_vol_ratio5"),
                rec.get("added_date"), rec.get("updated_date"), rec.get("last_date"),
                rec.get("last_close"), rec.get("last_volume"), rec.get("last_vol_ratio5"),
                rec.get("last_turnover"),
                self._json(rec.get("score")), self._json(rec.get("carry")),
                self._json(rec.get("launch")), self._json(rec.get("plan")),
                self._json(rec.get("history")), rec.get("next_trigger"),
                rec.get("quality"), rec.get("quality_note"), rec.get("risk_level"),
                rec.get("data_level") or "C", rec.get("source") or "tdx:vipdoc",
                rec.get("fetched_at") or _now(),
            ))
        if not payload:
            return 0
        written = 0
        with self.db.cursor() as cur:
            for row in payload:
                self._ensure_stock(cur, row[0], row[2], source="tdx:vipdoc")
                cur.execute(
                    """INSERT INTO limitup_pool
                       (symbol, code, name, market, board, sector, sector_tcode, state, stage,
                        first_board_date, first_board_close, first_board_volume,
                        first_board_amount, first_board_turnover, first_board_vol_ratio5,
                        added_date, updated_date, last_date, last_close, last_volume,
                        last_vol_ratio5, last_turnover, score_json, carry_json, launch_json,
                        plan_json, history_json, next_trigger, quality, quality_note,
                        risk_level, data_level, source, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (symbol) DO UPDATE SET
                           code = excluded.code,
                           name = COALESCE(excluded.name, limitup_pool.name),
                           market = excluded.market,
                           board = excluded.board,
                           sector = excluded.sector,
                           sector_tcode = excluded.sector_tcode,
                           state = excluded.state,
                           stage = excluded.stage,
                           first_board_date = COALESCE(excluded.first_board_date,
                                                       limitup_pool.first_board_date),
                           first_board_close = COALESCE(excluded.first_board_close,
                                                        limitup_pool.first_board_close),
                           first_board_volume = COALESCE(excluded.first_board_volume,
                                                         limitup_pool.first_board_volume),
                           first_board_amount = COALESCE(excluded.first_board_amount,
                                                         limitup_pool.first_board_amount),
                           first_board_turnover = COALESCE(excluded.first_board_turnover,
                                                           limitup_pool.first_board_turnover),
                           first_board_vol_ratio5 = COALESCE(excluded.first_board_vol_ratio5,
                                                             limitup_pool.first_board_vol_ratio5),
                           added_date = COALESCE(limitup_pool.added_date, excluded.added_date),
                           updated_date = excluded.updated_date,
                           last_date = excluded.last_date,
                           last_close = excluded.last_close,
                           last_volume = excluded.last_volume,
                           last_vol_ratio5 = excluded.last_vol_ratio5,
                           last_turnover = excluded.last_turnover,
                           score_json = COALESCE(excluded.score_json, limitup_pool.score_json),
                           carry_json = COALESCE(excluded.carry_json, limitup_pool.carry_json),
                           launch_json = COALESCE(excluded.launch_json, limitup_pool.launch_json),
                           plan_json = COALESCE(excluded.plan_json, limitup_pool.plan_json),
                           history_json = COALESCE(excluded.history_json,
                                                   limitup_pool.history_json),
                           next_trigger = excluded.next_trigger,
                           quality = excluded.quality,
                           quality_note = excluded.quality_note,
                           risk_level = excluded.risk_level,
                           data_level = excluded.data_level,
                           source = excluded.source,
                           fetched_at = excluded.fetched_at,
                           updated_at = datetime('now', 'localtime')""",
                    row,
                )
                written += cur.rowcount
        return written

    def limitup_pool(self, state: str | None = None) -> list[dict]:
        sql = "SELECT * FROM limitup_pool"
        params: list = []
        if state:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY updated_date DESC, symbol"
        with self.db.cursor() as cur:
            cur.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
        for item in rows:
            for key in ("score_json", "carry_json", "launch_json", "plan_json", "history_json"):
                raw = item.pop(key, None)
                if raw:
                    try:
                        item[key[:-5]] = json.loads(raw)
                    except (TypeError, ValueError):
                        item[key[:-5]] = None
        return rows

    def limitup_pool_summary(self) -> dict[str, int]:
        with self.db.cursor() as cur:
            cur.execute("SELECT state, COUNT(*) FROM limitup_pool GROUP BY state")
            return {str(state): int(count) for state, count in cur.fetchall()}

    def clear_limitup_pool(self, symbols: Iterable[str] | None = None) -> int:
        with self.db.cursor() as cur:
            if symbols is None:
                cur.execute("DELETE FROM limitup_pool")
            else:
                keys = [s for s in symbols]
                cur.executemany("DELETE FROM limitup_pool WHERE symbol = ?",
                                [(s,) for s in keys])
            return cur.rowcount

    # ================================================================ 龙虎榜
    @staticmethod
    def _ensure_stock(cur, symbol: str, name: str | None,
                      source: str = "eastmoney:lhb") -> None:
        """外键引用了 stock_info，先补齐代码表；名称只补不覆盖已有真实名称。"""
        code, market = symbol.split(".", 1)
        cur.execute(
            """INSERT OR IGNORE INTO stock_info (symbol, code, name, market, source)
               VALUES (?, ?, ?, ?, ?)""",
            (symbol, code, name or code, market, source),
        )
        if name:
            cur.execute(
                """UPDATE stock_info SET name = ?, updated_at = datetime('now', 'localtime')
                   WHERE symbol = ? AND (name IS NULL OR name = '' OR name = ?)""",
                (name, symbol, code),
            )

    def upsert_lhb(self, records: Iterable[dict]) -> int:
        """写入龙虎榜披露记录。幂等键 (symbol, trade_date, reason)，重复抓取只更新不新增。

        席位 JSON 允许为空：列表页拿不到席位，详情页才补齐；空值不会覆盖已有席位。
        """
        payload = []
        for rec in records:
            symbol = str(rec.get("symbol") or "").strip().upper()
            trade_date = rec.get("trade_date")
            if not symbol or "." not in symbol or not trade_date:
                continue
            reason = (rec.get("reason") or "").strip()
            payload.append((
                symbol, rec.get("name"), trade_date, reason,
                self._json(rec.get("buy_top5")), self._json(rec.get("sell_top5")),
                rec.get("net_buy"), rec.get("buy_amount"), rec.get("sell_amount"),
                rec.get("institution_net"), rec.get("change_pct"), rec.get("close_price"),
                rec.get("turnover"), rec.get("trade_market"), self._json(rec.get("post_perf")),
                rec.get("publish_date") or trade_date,
                rec.get("fetched_at") or _now(),
                rec.get("data_level") or "C", rec.get("quality_flag") or "disclosed",
                rec.get("source") or "eastmoney:lhb",
            ))
        if not payload:
            return 0
        written = 0
        with self.db.cursor() as cur:
            for row in payload:
                self._ensure_stock(cur, row[0], row[1])
                cur.execute(
                    """INSERT INTO dragon_tiger_records
                       (symbol, name, trade_date, reason, buy_top5, sell_top5, net_buy,
                        buy_amount, sell_amount, institution_net, change_pct, close_price,
                        turnover, trade_market, post_perf, publish_date, fetched_at,
                        data_level, quality_flag, source)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT (symbol, trade_date, reason) DO UPDATE SET
                           name = COALESCE(excluded.name, dragon_tiger_records.name),
                           buy_top5 = COALESCE(excluded.buy_top5, dragon_tiger_records.buy_top5),
                           sell_top5 = COALESCE(excluded.sell_top5, dragon_tiger_records.sell_top5),
                           net_buy = excluded.net_buy,
                           buy_amount = excluded.buy_amount,
                           sell_amount = excluded.sell_amount,
                           institution_net = COALESCE(excluded.institution_net,
                                                      dragon_tiger_records.institution_net),
                           change_pct = excluded.change_pct,
                           close_price = excluded.close_price,
                           turnover = excluded.turnover,
                           trade_market = excluded.trade_market,
                           post_perf = COALESCE(excluded.post_perf, dragon_tiger_records.post_perf),
                           publish_date = excluded.publish_date,
                           fetched_at = excluded.fetched_at,
                           data_level = excluded.data_level,
                           quality_flag = excluded.quality_flag,
                           source = excluded.source,
                           updated_at = datetime('now', 'localtime')""",
                    row,
                )
                written += cur.rowcount
        return written

    @staticmethod
    def _json(value) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value or None
        return json.dumps(value, ensure_ascii=False)

    @staticmethod
    def _decode_lhb(row) -> dict:
        item = dict(row)
        for key in ("buy_top5", "sell_top5", "post_perf"):
            raw = item.get(key)
            if raw:
                try:
                    item[key] = json.loads(raw)
                except (TypeError, ValueError):
                    item[key] = None
        return item

    def lhb_by_date(self, trade_date: str, limit: int = 500) -> list[dict]:
        """某日全市场榜单，按净买入降序。"""
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT * FROM dragon_tiger_records WHERE trade_date = ?
                   ORDER BY COALESCE(net_buy, 0) DESC, symbol LIMIT ?""",
                (trade_date, limit),
            )
            return [self._decode_lhb(row) for row in cur.fetchall()]

    def lhb_for_symbol(self, symbol: str, limit: int = 60) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT * FROM dragon_tiger_records WHERE symbol = ?
                   ORDER BY trade_date DESC, id DESC LIMIT ?""",
                (symbol, limit),
            )
            return [self._decode_lhb(row) for row in cur.fetchall()]

    def lhb_marks(self, symbol: str, days: int = 400) -> dict[str, dict]:
        """K 线上榜标记：{date: {reason, net, buy, sell, count}}。同一天多条原因合并计数。"""
        with self.db.cursor() as cur:
            cur.execute(
                """SELECT trade_date, reason, net_buy, buy_amount, sell_amount
                   FROM dragon_tiger_records
                   WHERE symbol = ? AND trade_date >= date('now', 'localtime', ?)
                   ORDER BY trade_date""",
                (symbol, f"-{int(days)} days"),
            )
            rows = cur.fetchall()
        out: dict[str, dict] = {}
        for row in rows:
            item = out.setdefault(row["trade_date"], {
                "count": 0, "reason": row["reason"], "net": 0.0, "buy": 0.0, "sell": 0.0})
            item["count"] += 1
            item["net"] += float(row["net_buy"] or 0.0)
            item["buy"] += float(row["buy_amount"] or 0.0)
            item["sell"] += float(row["sell_amount"] or 0.0)
        return out

    def lhb_dates(self, limit: int = 60) -> list[str]:
        with self.db.cursor() as cur:
            cur.execute("SELECT DISTINCT trade_date FROM dragon_tiger_records "
                        "ORDER BY trade_date DESC LIMIT ?", (limit,))
            return [row["trade_date"] for row in cur.fetchall()]

    def count_lhb(self) -> int:
        with self.db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM dragon_tiger_records")
            return int(cur.fetchone()["n"])

    # ================================================================ 复盘笔记
    def add_note(self, symbol: str | None, content: str, note_type: str = "observation",
                 trade_date: str | None = None) -> int:
        day = trade_date or datetime.now().strftime("%Y-%m-%d")
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO review_notes (symbol, trade_date, note_type, content)
                   VALUES (?, ?, ?, ?)""",
                (symbol, day, note_type, content),
            )
            return int(cur.lastrowid)

    def recent_notes(self, limit: int = 20) -> list[dict]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT * FROM review_notes ORDER BY trade_date DESC, id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cur.fetchall()]

    # ================================================================ 数据源状态
    def record_source_status(self, source: str, data_level: str, connected: bool,
                             latency_ms: int | None, error: str | None = None) -> None:
        with self.db.cursor() as cur:
            cur.execute(
                """INSERT INTO data_source_status
                   (source, data_level, connected, latency_ms, last_error, checked_at)
                   VALUES (?,?,?,?,?,datetime('now','localtime'))
                   ON CONFLICT(source, data_level) DO UPDATE SET
                       connected=excluded.connected, latency_ms=excluded.latency_ms,
                       last_error=excluded.last_error, checked_at=excluded.checked_at,
                       updated_at=datetime('now','localtime')""",
                (source, data_level, 1 if connected else 0, latency_ms, error),
            )
