"""股票池装载与搜索。

冷启动要求：5000+ 代码在 2 秒内可搜索、可展示，不阻塞窗口出现。
名称优先取通达信本地代码表（~0.2s 全市场），缺失才逐只回源补全。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from app.adapters.tdx_names import board_of, load_name_index, resolve_cache_dir
from app.db.repo import Repo

Progress = Callable[[str, int, int], None]


class PinyinIndex:
    """名称拼音索引：支持「gzmt」→ 贵州茅台。缺 pypinyin 时自动退化为空索引。"""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[str, str]] = {}
        self._ready = False
        try:
            import pypinyin  # noqa: F401
            self._has_pinyin = True
        except ImportError:
            self._has_pinyin = False

    @property
    def available(self) -> bool:
        return self._has_pinyin and self._ready

    def build(self, rows: list[dict]) -> None:
        if not self._has_pinyin:
            return
        from pypinyin import Style, lazy_pinyin

        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            initials = "".join(lazy_pinyin(name, style=Style.FIRST_LETTER, errors="ignore")).lower()
            full = "".join(lazy_pinyin(name, errors="ignore")).lower()
            self._entries[row["symbol"]] = (initials, full)
        self._ready = True

    def match(self, query: str, limit: int = 40) -> list[str]:
        if not self.available:
            return []
        q = query.lower()
        scored: list[tuple[int, str]] = []
        for symbol, (initials, full) in self._entries.items():
            if initials.startswith(q):
                scored.append((0, symbol))
            elif q in initials:
                scored.append((1, symbol))
            elif full.startswith(q):
                scored.append((2, symbol))
            elif q in full:
                scored.append((3, symbol))
            if len(scored) > limit * 6:
                break
        scored.sort()
        return [symbol for _, symbol in scored[:limit]]


class UniverseService:
    def __init__(self, repo: Repo, adapter=None, config: dict | None = None) -> None:
        self.repo = repo
        self.adapter = adapter
        self.config = config or {}
        self.pinyin = PinyinIndex()
        self.last_load: dict = {}
        self._load_lock = threading.Lock()

    # ---------------------------------------------------------------- 装载
    def cache_dir(self) -> Path | None:
        ds = self.config.get("datasource") or {}
        explicit = ds.get("tdx_cache_dir")
        if explicit:
            path = Path(explicit)
            return path if path.is_dir() else None
        user_dir = ds.get("user_dir")
        return resolve_cache_dir(user_dir) if user_dir else None

    def load(self, progress: Progress | None = None) -> dict:
        """载入全市场代码。返回 {count, named, elapsed_ms, name_source, error}。"""
        started = time.time()
        with self._load_lock:
            codes: list[str] = []
            error = None
            if self.adapter is not None:
                try:
                    raw = self.adapter.get_stock_list()
                    codes = [getattr(item, "symbol", item) for item in raw]
                except Exception as exc:  # 数据源不可用不应阻止界面打开
                    error = f"{type(exc).__name__}: {exc}"
            if progress:
                progress("已取到代码", 0, len(codes))

            cache_dir = self.cache_dir()
            names = load_name_index(cache_dir)
            name_source = "tdx_tnf" if names else "none"

            rows = []
            for idx, symbol in enumerate(codes):
                symbol = str(symbol).strip().upper()
                if "." not in symbol:
                    continue
                code, market = symbol.split(".", 1)
                rows.append({
                    "symbol": symbol,
                    "name": names.get(symbol) or code,
                    "board": board_of(code) if market != "BJ" else "北交所",
                    "source": name_source if symbol in names else "tqcenter",
                })
                if progress and idx and idx % 1000 == 0:
                    progress("写入代码表", idx, len(codes))

            written = self.repo.upsert_stocks(rows) if rows else 0
            if not written and self.repo.stock_count() == 0:
                # 数据源与本地表都拿不到时才回落到库里已有数据
                pass
            self.last_load = {
                "count": self.repo.stock_count(),
                "named": sum(1 for r in rows if r["name"] != r["symbol"].split(".")[0]),
                "elapsed_ms": int((time.time() - started) * 1000),
                "name_source": name_source,
                "error": error,
            }
            if progress:
                progress("完成", len(codes), len(codes))
            return self.last_load

    def load_async(self, progress: Progress | None = None,
                   done: Callable[[dict], None] | None = None) -> threading.Thread:
        def worker() -> None:
            summary = self.load(progress)
            self.build_pinyin_async()
            if done:
                done(summary)

        thread = threading.Thread(target=worker, name="universe-load", daemon=True)
        thread.start()
        return thread

    def build_pinyin_async(self) -> threading.Thread:
        def worker() -> None:
            self.pinyin.build(self.repo.all_stocks())

        thread = threading.Thread(target=worker, name="pinyin-build", daemon=True)
        thread.start()
        return thread

    # ---------------------------------------------------------------- 搜索
    def search(self, query: str, limit: int = 40) -> list[dict]:
        q = (query or "").strip()
        if not q:
            return []
        results = self.repo.search(q, limit)
        seen = {row["symbol"] for row in results}
        if q.isascii() and q.replace(".", "").isalnum() and len(q) <= 12:
            extra = [s for s in self.pinyin.match(q, limit) if s not in seen]
            if extra:
                names = self.repo.stock_names(extra)
                for symbol in extra:
                    code, market = symbol.split(".", 1)
                    results.append({
                        "symbol": symbol, "code": code, "market": market,
                        "name": names.get(symbol, code), "board": board_of(code),
                    })
        return results[:limit]

    def display_names(self, symbols: list[str]) -> dict[str, str]:
        return self.repo.stock_names(symbols)

    # ---------------------------------------------------------------- 名称补全
    def missing_name_symbols(self, limit: int = 2000) -> list[str]:
        rows = self.repo.all_stocks()
        return [r["symbol"] for r in rows if r["name"] == r["code"]][:limit]

    def fill_missing_names(self, limit: int = 2000, progress: Progress | None = None) -> int:
        """名称表缺失时的慢速兜底：逐只取快照名称。约 11ms/只，放在后台线程跑。"""
        if self.adapter is None:
            return 0
        filled = 0
        for idx, symbol in enumerate(self.missing_name_symbols(limit)):
            try:
                quote = self.adapter.get_quote(symbol)
            except Exception:
                continue
            if quote.name:
                self.repo.upsert_stocks([{
                    "symbol": symbol, "name": quote.name,
                    "board": board_of(symbol.split(".")[0]), "source": "tqcenter",
                }])
                filled += 1
            if progress and idx % 50 == 0:
                progress("补全名称", idx, limit)
        return filled
