"""通达信本地代码表（shs.tnf / szs.tnf / bjs.tnf）解析。

为什么需要它：tqcenter 只给代码不给名称，逐只 `get_stock_info` 取名称实测 10.5ms/只，
5000 只要 52s，冷启动不可接受。本地 tnf 表解析全市场名称约 0.2s。

格式（实测）：文件头 50 字节，之后每条记录 360 字节；
  记录内 0..6 为 6 位代码（ASCII），31..39 为名称（GBK，右补 \x00）。
文件缺失或长度不整除时返回空字典，调用方回落到逐只请求，绝不猜名称。
"""
from __future__ import annotations

from pathlib import Path

RECORD_SIZE = 360
HEADER_SIZE = 50
NAME_OFFSET = 31
NAME_LENGTH = 8

BOARD_RULES: list[tuple[tuple[str, ...], str]] = [
    (("600", "601", "603", "605"), "沪市主板"),
    (("688", "689"), "科创板"),
    (("000", "001"), "深市主板"),
    (("002", "003"), "深市主板"),
    (("300", "301", "302"), "创业板"),
    (("920", "430", "830", "831", "832", "833", "834", "835", "836", "837",
      "838", "839", "870", "871", "872", "873", "874", "875"), "北交所"),
]


def board_of(code: str) -> str:
    for prefixes, board in BOARD_RULES:
        if code.startswith(prefixes):
            return board
    return "其他"


def resolve_cache_dir(user_dir: str | Path) -> Path | None:
    """由 tqcenter 的 user 目录反推 T0002/hq_cache。找不到返回 None。"""
    path = Path(user_dir)
    for base in (path, *path.parents):
        candidate = base / "T0002" / "hq_cache"
        if candidate.is_dir():
            return candidate
    return None


def _decode_name(raw: bytes) -> str:
    return raw.split(b"\x00", 1)[0].decode("gbk", "ignore").strip().replace(" ", "").replace("\u3000", "")


def parse_tnf(path: Path, market: str) -> dict[str, str]:
    """解析单个 tnf，返回 {symbol: name}。格式异常返回空字典。"""
    try:
        blob = path.read_bytes()
    except OSError:
        return {}
    body = len(blob) - HEADER_SIZE
    if body <= 0 or body % RECORD_SIZE:
        return {}
    out: dict[str, str] = {}
    for i in range(body // RECORD_SIZE):
        start = HEADER_SIZE + i * RECORD_SIZE
        record = blob[start:start + RECORD_SIZE]
        code = record[0:6].decode("ascii", "ignore").strip()
        if len(code) != 6 or not code.isdigit():
            continue
        name = _decode_name(record[NAME_OFFSET:NAME_OFFSET + NAME_LENGTH])
        if name:
            out[f"{code}.{market}"] = name
    return out


def load_name_index(cache_dir: Path | None) -> dict[str, str]:
    """读取 sh/sz/bj 三张表并合并。任一文件缺失只是少一部分，不抛异常。"""
    if cache_dir is None or not cache_dir.is_dir():
        return {}
    merged: dict[str, str] = {}
    for filename, market in (("shs.tnf", "SH"), ("szs.tnf", "SZ"), ("bjs.tnf", "BJ")):
        merged.update(parse_tnf(cache_dir / filename, market))
    return merged
