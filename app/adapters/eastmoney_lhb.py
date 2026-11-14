"""东方财富龙虎榜适配器（交易所公开披露数据，C 级 · T 日盘后披露）。

数据页：https://data.eastmoney.com/stock/lhb.html
接口：https://datacenter-web.eastmoney.com/api/data/v1/get（三个报表）

| 用途 | reportName |
|---|---|
| 当日榜单 / 个股历史 | `RPT_DAILYBILLBOARD_DETAILSNEW` |
| 买入前五营业部 | `RPT_BILLBOARD_DAILYDETAILSBUY` |
| 卖出前五营业部 | `RPT_BILLBOARD_DAILYDETAILSSELL` |

口径与铁律：
- 这是**披露数据**，不是行情：等级一律 C，`publish_date` 必须落库（T 日收盘后发布，次日才稳定）。
- 只搬运交易所公布的原因与营业部名称，**不推断席位身份**（「机构专用」是官方席位名，原样保留）。
- 字段单位：金额为元；涨跌幅/换手率为百分数。缺失保持 None，界面显示 `--`，不补 0。
- 任何异常都转成 `DataSourceError`，由调用方降级，不静默返回空列表冒充「当日无上榜」。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from app.adapters.base import DataSourceError

BASE_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SOURCE = "eastmoney:lhb"
DATA_LEVEL = "C"
PAGE_URL = "https://data.eastmoney.com/stock/lhb.html"

DAILY_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
BUY_REPORT = "RPT_BILLBOARD_DAILYDETAILSBUY"
SELL_REPORT = "RPT_BILLBOARD_DAILYDETAILSSELL"

INSTITUTION_SEAT = "机构专用"

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _num(value) -> float | None:
    """东财的 null 一律转 None，避免把「没数据」写成 0。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value) -> str | None:
    if not value:
        return None
    text = str(value)
    return text[:10] if len(text) >= 10 else None


class EastmoneyLhbAdapter:
    """龙虎榜取数器。只读公开接口，不带任何账号信息。"""

    def __init__(self, config: dict | None = None) -> None:
        cfg = (config or {}).get("lhb") or {}
        self.timeout = float(cfg.get("timeout_sec", 12))
        self.retries = int(cfg.get("retries", 2))
        self.page_size = int(cfg.get("page_size", 100))
        self.retry_sleep = float(cfg.get("retry_sleep_sec", 0.8))
        self.source = SOURCE
        self.data_level = DATA_LEVEL

    # ---------------------------------------------------------------- HTTP
    def _request(self, params: dict) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{BASE_URL}?{query}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": _UA,
                    "Referer": PAGE_URL,
                    "Accept": "application/json, text/plain, */*",
                })
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if not payload.get("success", False):
                    # 接口用 success=false 表达「排序列不存在」这类参数错误，不是网络问题
                    raise DataSourceError(
                        kind="bad_request",
                        message=f"东方财富接口拒绝请求：{payload.get('message') or payload.get('code')}",
                        retryable=False,
                    )
                return payload
            except DataSourceError:
                raise
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
                last = exc
                if attempt < self.retries:
                    time.sleep(self.retry_sleep * (attempt + 1))
        raise DataSourceError(
            kind="network",
            message=f"龙虎榜取数失败（{type(last).__name__}: {last}）。"
                    f"检查网络或代理；离线时可用已落库的历史数据。",
            retryable=True,
        )

    def _query(self, report: str, flt: str, *, sort: str, pages: int = 1,
               page_size: int | None = None) -> list[dict]:
        size = page_size or self.page_size
        rows: list[dict] = []
        for page in range(1, pages + 1):
            payload = self._request({
                "reportName": report,
                "columns": "ALL",
                "filter": flt,
                "pageNumber": page,
                "pageSize": size,
                "sortColumns": sort,
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            })
            result = payload.get("result") or {}
            batch = result.get("data") or []
            rows.extend(batch)
            if len(batch) < size:
                break
        return rows

    # ---------------------------------------------------------------- 归一化
    def _record(self, row: dict) -> dict | None:
        symbol = (row.get("SECUCODE") or "").upper()
        trade_date = _day(row.get("TRADE_DATE"))
        if not symbol or not trade_date:
            return None
        return {
            "symbol": symbol,
            "name": row.get("SECURITY_NAME_ABBR"),
            "trade_date": trade_date,
            "reason": row.get("EXPLANATION") or row.get("EXPLAIN") or None,
            "net_buy": _num(row.get("BILLBOARD_NET_AMT")),
            "buy_amount": _num(row.get("BILLBOARD_BUY_AMT")),
            "sell_amount": _num(row.get("BILLBOARD_SELL_AMT")),
            "change_pct": _num(row.get("CHANGE_RATE")),
            "close_price": _num(row.get("CLOSE_PRICE")),
            "turnover": _num(row.get("TURNOVERRATE")),
            "trade_market": row.get("TRADE_MARKET"),
            "post_perf": self._post_perf(row),
            "publish_date": trade_date,      # T 日收盘后披露
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "source": self.source,
            "data_level": self.data_level,
            "quality_flag": "disclosed",
        }

    @staticmethod
    def _post_perf(row: dict) -> dict | None:
        mapping = {"d1": "D1_CLOSE_ADJCHRATE", "d2": "D2_CLOSE_ADJCHRATE",
                   "d5": "D5_CLOSE_ADJCHRATE", "d10": "D10_CLOSE_ADJCHRATE",
                   "d20": "D20_CLOSE_ADJCHRATE", "d30": "D30_CLOSE_ADJCHRATE"}
        perf = {key: _num(row.get(col)) for key, col in mapping.items()}
        return perf if any(v is not None for v in perf.values()) else None

    # ---------------------------------------------------------------- 对外
    def latest_trade_date(self) -> str | None:
        """最近一个有龙虎榜的交易日（不是「今天」，节假日自动跳过）。"""
        payload = self._request({
            "reportName": DAILY_REPORT, "columns": "ALL", "filter": "",
            "pageNumber": 1, "pageSize": 1,
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "source": "WEB", "client": "WEB",
        })
        rows = ((payload.get("result") or {}).get("data")) or []
        return _day(rows[0].get("TRADE_DATE")) if rows else None

    def daily_board(self, trade_date: date | str) -> list[dict]:
        day = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        rows = self._query(DAILY_REPORT, f"(TRADE_DATE='{day}')",
                           sort="BILLBOARD_NET_AMT", pages=6)
        return [rec for rec in (self._record(r) for r in rows) if rec]

    def symbol_history(self, symbol: str, limit: int = 60) -> list[dict]:
        code = symbol.split(".")[0]
        rows = self._query(DAILY_REPORT, f'(SECURITY_CODE="{code}")',
                           sort="TRADE_DATE", pages=1, page_size=max(1, min(limit, self.page_size)))
        return [rec for rec in (self._record(r) for r in rows) if rec]

    def seats(self, trade_date: date | str, symbol: str) -> dict:
        """买卖前五营业部。金额单位元，营业部名称原样保留（「机构专用」不翻译、不推断）。"""
        day = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        code = symbol.split(".")[0]
        flt = f'(TRADE_DATE=\'{day}\')(SECURITY_CODE="{code}")'
        buy_rows = self._query(BUY_REPORT, flt, sort="BUY", pages=1, page_size=10)
        sell_rows = self._query(SELL_REPORT, flt, sort="SELL", pages=1, page_size=10)
        buy = [self._seat(r, "buy") for r in buy_rows]
        sell = [self._seat(r, "sell") for r in sell_rows]
        return {
            "buy": [s for s in buy if s],
            "sell": [s for s in sell if s],
            "institution_net": self._institution_net(buy, sell),
        }

    @staticmethod
    def _seat(row: dict, side: str) -> dict | None:
        name = row.get("OPERATEDEPT_NAME")
        if not name:
            return None
        amount = _num(row.get("BUY" if side == "buy" else "SELL"))
        return {
            "side": side,
            "name": name,
            "buy": _num(row.get("BUY")),
            "sell": _num(row.get("SELL")),
            "net": _num(row.get("NET")),
            "amount": amount,
            "trade_id": row.get("TRADE_ID"),
        }

    @staticmethod
    def _institution_net(buy: list[dict], sell: list[dict]) -> float | None:
        """「机构专用」席位的净额。这是官方席位名的加总，不是对资金身份的推断。"""
        values = [s for s in list(buy) + list(sell) if s and s.get("name") == INSTITUTION_SEAT]
        if not values:
            return None
        total = 0.0
        for seat in values:
            total += (seat.get("buy") or 0.0) - (seat.get("sell") or 0.0)
        return round(total, 2)

    def fetch_detail(self, trade_date: date | str, symbol: str) -> dict:
        """某股某日的完整记录：榜单行 + 买卖席位。用于窗口右侧详情。"""
        day = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        code = symbol.split(".")[0]
        rows = self._query(DAILY_REPORT, f"(TRADE_DATE='{day}')(SECURITY_CODE=\"{code}\")",
                           sort="BILLBOARD_NET_AMT", pages=1, page_size=20)
        records = [rec for rec in (self._record(r) for r in rows) if rec]
        seat_data = self.seats(day, symbol)
        for rec in records:
            rec["buy_top5"] = seat_data["buy"]
            rec["sell_top5"] = seat_data["sell"]
            rec["institution_net"] = seat_data["institution_net"]
        return {"date": day, "symbol": symbol, "records": records, **seat_data}
