from .base import (
    DataSourceError,
    Capabilities,
    MarketDataAdapter,
    TradeDataAdapter,
    IndustryDataAdapter,
    DragonTigerAdapter,
)
from .mock import MockAdapter

__all__ = [
    "DataSourceError",
    "Capabilities",
    "MarketDataAdapter",
    "TradeDataAdapter",
    "IndustryDataAdapter",
    "DragonTigerAdapter",
    "MockAdapter",
    "build_adapter",
    "build_lhb_adapter",
    "build_intraday_adapter",
]


def build_adapter(config: dict):
    """按配置装配适配器。新增供应商时只改这里，UI 与服务层不动。"""
    name = (config.get("datasource") or {}).get("name", "tqcenter")
    if name == "mock":
        return MockAdapter()
    if name == "tqcenter":
        from app.adapters.tqcenter_adapter import TqCenterAdapter  # 延迟导入，避免强依赖
        return TqCenterAdapter(config)
    raise DataSourceError(
        kind="not_implemented",
        message=f"数据源 {name} 尚未实现，请在 app/adapters/ 下新增适配器后在 build_adapter 注册",
        retryable=False,
    )


def build_lhb_adapter(config: dict):
    """龙虎榜是独立数据源（交易所披露走东方财富），与行情源解耦。

    `datasource.enable_lhb` 或 `lhb.enabled` 为真才装配；关闭时返回 None，界面不显示入口，
    而不是给一个永远拉不到数据的空面板。
    """
    ds = config.get("datasource") or {}
    lhb_cfg = config.get("lhb") or {}
    enabled = bool(lhb_cfg.get("enabled", ds.get("enable_lhb", False)))
    if not enabled:
        return None
    provider = (lhb_cfg.get("provider") or "eastmoney").lower()
    if provider != "eastmoney":
        raise DataSourceError(
            kind="not_implemented",
            message=f"龙虎榜数据源 {provider} 未实现，当前仅支持 eastmoney",
            retryable=False,
        )
    from app.adapters.eastmoney_lhb import EastmoneyLhbAdapter
    return EastmoneyLhbAdapter(config)


def build_intraday_adapter(config: dict):
    """分时（分钟线）数据源：东财或 free-stockdb 本地库，与行情源解耦。

    为什么不再提供 `snapshot` 开关：快照差分是 3 秒区间的另一套口径（点数稀疏、
    依赖轮询间隔），一旦作为分时的回落源，界面上就会出现「同一张图两种口径」，
    也解释不了「为什么今天只有 5 个点」。分时必须是真正的分钟线聚合，
    取数失败就如实报错 + 显示库内最近一个交易日，不做静默降级。

    当前支持：

    - `eastmoney`（默认）：公开 trends2 接口，241 点，需联网。
    - `free_stockdb`：读本机 free-stockdb（`127.0.0.1:7899`）的 `分钟k` 表，
      不限频、可离线查询，但数据新鲜度取决于同步工具是否跑过。
      默认开启 `fallback_eastmoney`：主源不可用（引擎没起、当日未同步）时
      自动回落东财，回落原因记在 FallbackIntradayAdapter.last_reason 里。

    旧配置里的 `snapshot/none/off` 仍接受但一律按 eastmoney 装配，避免升级后
    直接起不来；未知 provider 仍然报错，免得把拼写错误吞掉。
    """
    cfg = config.get("intraday") or {}
    provider = (cfg.get("provider") or "eastmoney").lower()

    if provider == "free_stockdb":
        from app.adapters.eastmoney_intraday import EastmoneyIntradayAdapter
        from app.adapters.fallback import FallbackIntradayAdapter
        from app.adapters.free_stockdb_intraday import FreeStockDbIntradayAdapter

        fcfg = cfg.get("free_stockdb") or {}
        primary = FreeStockDbIntradayAdapter(config)
        if not bool(fcfg.get("fallback_eastmoney", True)):
            return primary
        return FallbackIntradayAdapter(
            primary,
            EastmoneyIntradayAdapter(config),
            cooldown_sec=float(fcfg.get("fallback_cooldown_sec", 120)),
        )

    if provider not in ("eastmoney", "snapshot", "none", "off", "disabled"):
        raise DataSourceError(
            kind="not_implemented",
            message=(f"分时数据源 {provider} 未实现，"
                     f"当前仅支持 eastmoney / free_stockdb"),
            retryable=False,
        )
    from app.adapters.eastmoney_intraday import EastmoneyIntradayAdapter
    return EastmoneyIntradayAdapter(config)
