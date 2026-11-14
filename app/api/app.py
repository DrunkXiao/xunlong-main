"""FastAPI 应用：供桌面端与外部桥接调用。"""
from __future__ import annotations

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.adapters import build_adapter
from app.config import load_config, sqlite_path_from_url
from app.db.session import Database
from app.services.alert_engine import AlertEngine
from app.services.tdx_bridge import ingest, read_csv

app = FastAPI(title="寻龙盯盘 本地服务", version="0.1.0")
config = load_config()
adapter = build_adapter(config)
engine = AlertEngine()


def _database() -> Database:
    path = sqlite_path_from_url(config["database"]["url"])
    return Database(path)


def _ingest_with_db(payload: dict, channel: str) -> dict:
    database = _database()
    with database.cursor() as cur:
        return ingest(cur, payload, channel=channel)


@app.get("/health")
def health() -> dict:
    caps = adapter.capabilities
    return {
        "status": "ok",
        "datasource": config.get("datasource", {}).get("name"),
        "data_level": caps.data_level,
        "realtime": caps.realtime,
        "tick": caps.tick,
        "order_book_levels": caps.order_book_levels,
        "note": caps.note,
    }


@app.get("/api/quotes")
def quotes(symbols: str = "") -> list[dict]:
    wanted = [s for s in symbols.split(",") if s] or [q.symbol for q in adapter.get_stock_list()]
    return [
        {
            "symbol": q.symbol, "name": q.name, "price": q.price,
            "change_pct": q.change_pct, "amount": q.amount,
            "data_level": q.data_level, "source": q.source,
            "quote_time": q.quote_time.isoformat() if q.quote_time else None,
        }
        for q in adapter.get_quotes(wanted).values()
    ]


@app.post("/api/tdx/ingest")
def tdx_ingest(payload: dict) -> dict:
    """通达信桥接 HTTP 通道：写入分组、去重、记录 tdx_events。"""
    return _ingest_with_db(payload, channel="http")


@app.post("/api/tdx/csv")
def tdx_csv(path: str, group: str = "盘中异动池") -> dict:
    """通达信 CSV 通道：导入导出文件。"""
    return _ingest_with_db(read_csv(path, group), channel="csv")


@app.websocket("/ws/quotes")
async def ws_quotes(ws: WebSocket) -> None:
    await ws.accept()
    try:
        while True:
            symbols = (await ws.receive_text()).split(",")
            await ws.send_json([
                {"symbol": q.symbol, "price": q.price, "change_pct": q.change_pct,
                 "data_level": q.data_level}
                for q in adapter.get_quotes([s for s in symbols if s]).values()
            ])
    except WebSocketDisconnect:
        return
