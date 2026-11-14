"""配置加载：config.yaml -> dict，缺文件时回落到安全默认值。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "app": {"name": "寻龙盯盘", "data_dir": "./data", "log_level": "INFO"},
    "database": {"url": "sqlite:///./data/dragon_hunter.db"},
    "datasource": {
        "name": "tqcenter",
        "data_level": "A",
        "poll_interval_sec": 3,
        "latency_warn_ms": 5000,
        "latency_abort_ms": 30000,
        "user_dir": r"D:/new_tdx64/PYPlugins/user",
        "strategy_name": "",
        "universe": ["SH_MAIN", "SH_STAR", "SZ_MAIN", "CHINEXT", "BJ"],
        "more_cache_ttl_sec": 2.0,
        "enable_tick": False,
        "enable_minute_kline": False,
        "enable_lhb": False,
    },
    "market": {"heartbeat_sec": 5, "calendar_source": "approximate"},
    "large_order": {
        "tiers": {"normal": 1000000, "large": 3000000, "super": 5000000, "continuous": 10000000},
        "streak_count": 3,
        "streak_amount_multiplier": 2,
    },
    "alerts": {"default_cooldown_sec": 300, "per_symbol_per_minute_limit": 5,
               "require_trading": True},
    # 强势回调选股：默认值即「能跑起来」的最小口径，具体参数放 config.yaml 的 pullback 段
    "pullback": {
        "tdx_root": "",
        "markets": ["SH", "SZ"],
        "universe_prefixes": ["60", "00", "30"],
        "exclude_st": True,
        "min_bars": 60,
        "tail_bars": 160,
        "stale_days_max": 12,
        "stale_stock_days": 7,
        "account_equity": 1_000_000,
        "max_candidates": 120,
        "report_top_n": 60,
        "limit": 0,
        "strategy": {
            "recent_limitup_days": 10, "stage_high_lookback": 20,
            "support_tolerance_pct": 1.5, "pullback_volume_ratio_max": 0.5,
            "pullback_window": 5, "rally_window": 10, "stop_buffer_pct": 2.0,
            "reward_risk_ratio": 2.5, "limitup_close_only": True,
            "sector_score_mode": "off", "risk_pct": 1.0, "position_pct": 10.0,
            "max_positions": 3,
        },
    },
    "tdx_bridge": {"enabled": False, "default_group": "盘中异动池", "http_port": 8611},
    # 通达信联动按钮：把当前标的 6 位代码 GET 给本机桥接服务（外部脚本监听 3000）
    "tdx_link": {"base_url": "http://127.0.0.1:3000", "timeout_sec": 2.0},
    # 涨停四阶段选股：阈值写在 app/services/limitup.py（与 strategy 文档一一对应），
    # 这里只放扫描范围与数据闸门
    "limitup": {
        "tdx_root": "",
        "markets": ["SH", "SZ"],
        "universe_prefixes": ["60", "00", "30"],
        "exclude_st": True,
        "min_bars": 25,
        "tail_bars": 130,
        "board_min_days": 60,
        "top": 10,
        "stale_days_max": 10,
        "max_idle_days": 12,
        "report_top_n": 60,
        "limit": 0,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | Path | None = "config.yaml") -> dict[str, Any]:
    """读取 YAML 配置。文件缺失或 PyYAML 未安装时返回默认值，不抛异常。"""
    config = dict(DEFAULTS)
    if path is None:
        return config
    file = Path(path)
    if not file.exists():
        return config
    try:
        import yaml  # 延迟导入，缺依赖时仍可启动
    except ImportError:
        return config
    with file.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if isinstance(loaded, dict):
        config = _deep_merge(config, loaded)
    return config


def sqlite_path_from_url(url: str) -> Path:
    """从 sqlite:///./data/x.db 解析出文件路径。"""
    if not url.startswith("sqlite:///"):
        raise ValueError(f"仅支持 sqlite:/// URL，收到: {url}")
    raw = url[len("sqlite:///"):]
    return Path(raw)
