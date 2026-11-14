# 数据接口与适配器

## 五类协议

业务层只依赖下列协议。任何供应商实现放在 `app/adapters/`，通过 `config.yaml` 的 `datasource.name` 装配。

```python
class MarketDataAdapter(Protocol):
    def get_stock_list(self) -> list[StockInfo]: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def get_quotes(self, symbols: list[str]) -> dict[str, Quote]: ...
    def get_kline(self, symbol: str, period: str, start: datetime | None,
                  end: datetime | None, limit: int = 500) -> list[Bar]: ...
    async def subscribe_quotes(self, symbols: list[str]) -> None: ...
    async def unsubscribe_quotes(self, symbols: list[str]) -> None: ...

class TradeDataAdapter(Protocol):
    def get_ticks(self, symbol: str, start: datetime, end: datetime) -> list[Tick]: ...
    async def subscribe_ticks(self, symbol: str) -> None: ...
    def get_order_book(self, symbol: str) -> OrderBook | None: ...

class IndustryDataAdapter(Protocol):
    def get_industry(self, symbol: str) -> IndustryInfo: ...
    def get_concepts(self, symbol: str) -> list[Concept]: ...
    def get_sector_quotes(self, sector: str) -> SectorQuote: ...
    def get_sector_constituents(self, sector: str) -> list[str]: ...

class DragonTigerAdapter(Protocol):
    def get_lhb_records(self, symbol: str, start: date, end: date) -> list[LhbRecord]: ...
    def get_lhb_detail(self, trade_date: date, symbol: str) -> LhbDetail | None: ...

class DataSourceManager(Protocol):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    def health_check(self) -> HealthStatus: ...
    def get_latency(self) -> int: ...          # ms
    def get_data_timestamp(self) -> datetime: ...
```

所有返回值带 `source: str`、`data_level: str`、`timestamp: datetime`。适配器内部异常一律转换为 `DataSourceError(kind, message, retryable)`，禁止把供应商原始异常抛到 UI。

**调用前三道闸门**（tqcenter 实测得出的硬约束）：

1. 本地代码校验：tqcenter 收到非法代码会内部 `tq.close()` 关闭整条连接，必须先过正则+市场前缀一致性检查。
2. 能力校验：先读 `capabilities`，不支持的周期/功能直接抛 `DataSourceError(kind='unsupported')`，不要发请求试运气。
3. 输出静音：tqcenter 用 `print` 报错，所有调用包一层 `redirect_stdout`，否则日志被刷屏且异常判断失真。

tqcenter 的完整实测矩阵、单位换算与性能数据见 `references/tqcenter.md`。

## 数据源分级

| 级 | 含义 | 界面标识 |
|---|---|---|
| A | 授权实时行情 | 顶部「实时」绿点 + 延迟 ms |
| B | 可用延时行情 | 「延时 {n} 分钟」黄标 |
| C | 历史 / 盘后数据 | 「历史」灰标 |
| D | 模拟测试数据 | 「模拟数据」红色条纹水印，禁止与真实数据同屏混用 |

规则：

- 默认数据源 tqcenter 的通达信本地快照 `DelayMin=0`，按 A 级处理；但它**没有逐笔、没有分钟K**，
  这三处必须显式标不支持，细则见 `references/tqcenter.md`。
- 免费源通常只能提供 B/C 级：不得声明支持实时逐笔、完整五档、实时大单。
- 同一界面若混用不同等级数据，每个面板角落单独标注等级。
- 数据源切换时先 `unsubscribe` 全部再重新订阅，避免重复推送。
- 可评估接入：Tushare、AkShare、券商行情 API、通达信本地数据（默认）、用户授权的逐笔/盘口接口。
  接入前先探测该源实际能给出的字段，生成能力矩阵写入 README。

能力探测结果必须显式暴露为 `capabilities = {tick: bool, order_book_levels: int, realtime: bool}`，UI 据此决定是否渲染对应面板，而不是渲染空面板。

## 时间戳与去重

- 存储统一 ISO8601，毫秒精度，`Asia/Shanghai`。数据库内保留交易所时间 `quote_time` 与本地接收时间 `received_at`。
- 延迟 = `received_at - quote_time`；超过阈值（默认 5s，可配）标黄，超过 30s 标红并暂停信号。
- 逐笔去重键 `(symbol, trade_time, price, volume, direction)`；批量写入用 `INSERT OR IGNORE`。
- 多源合并：价格与数量一致但方向冲突 → 方向降级 `unknown`；时间差 < 1ms 且价格数量一致 → 视为重复，保留先到源并记 `merged`。
- 缺口检测：分时按 1 分钟桶聚合，某桶成交为 0 而前后非 0 时标记「疑似缺失」而非 0，图中断点显示。

## 通达信桥接协议

独立进程/线程，通道可插拔：`csv` / `json_watch` / `http` / `ws` / `dde`。

标准事件 JSON：

```json
{
  "source": "tongdaxin",
  "event": "watchlist_update",
  "timestamp": "2026-11-08T10:30:00",
  "symbols": [
    {"code": "600000.SH", "name": "示例股票", "group": "龙头观察"}
  ]
}
```

`event` 取值：`watchlist_update` / `selection_result` / `formula_signal` / `current_symbol`。

处理流程：

1. 代码标准化（`600000` → `600000.SH`；`000001` → `000001.SZ`；`300xxx` → `.SZ`；`688xxx` → `.SH`；`8/4` 开头 → 北交所标注不支持则不导入）。
2. 校验存在于 `stock_info`，不存在则先拉基础信息，失败则记 `tdx_events.error` 并跳过。
3. 组不存在则按 `group` 名创建。
4. 批量去重后写入 `watch_group_stocks`，冲突忽略。
5. 全量写 `tdx_events`（原始 payload、条数、新增条数、成功与否、错误）。
6. 通过 `/ws/tdx` 推送 UI 刷新。

连接状态机：`disconnected → connecting → connected → degraded → reconnecting`。重连指数退避 1/2/4/8/16s，上限 30s，每次重连写日志；连续失败 5 次后进入 `degraded` 并在状态条提示，需用户手动恢复或等待下一次成功。

禁止假设各版本通达信协议一致：每个通道一个适配器，能力在 README 中逐版本说明。

## 分时（分钟线）来源

分时是独立协议，与 `datasource.name` 解耦，由 `intraday.provider` 装配
（`app/adapters/__init__.py::build_intraday_adapter`）。两个实现必须交出同一个形状
（`app/adapters/minute.py` 的 `Trends` / `MinuteBar`），否则 `MinuteFeed`、
大单识别、分时图、落库都要写两套分支。

| provider | 实现 | 数据 | 等级 | 依赖 |
|---|---|---|---|---|
| `eastmoney`（默认） | `eastmoney_intraday.py` | trends2 接口，241 点 | B | 联网 |
| `free_stockdb` | `free_stockdb_intraday.py` | 本机 free-stockdb `分钟k` 表 | C | 引擎 + 已同步数据 |

### free-stockdb 取数要点（走 Python SDK，不是 HTTP）

- 引擎 `stockdb.exe` 监听 `127.0.0.1:7899`，但回包是**二进制打包协议，不是 JSON**。
  自己拼 `?cmd=get&t=...` 再 `json.loads` 一定 `JSONDecodeError`——这是分时页曾经空白的原因。
  Python 侧唯一入口是 `from stock_sdk import rd, bk, zb`（见
  `http://a.123128.xyz/docs/AI策略python开发接口文档.md`）。
- 适配器只用三个调用（`app/adapters/free_stockdb_intraday.py` 的 `SdkReader`）：

  | 目的 | 调用 |
  |---|---|
  | 当天整日分钟 | `rd.get_data(code, start="20261113", end="20261113", frequency="1m", fq=None)` |
  | 最新一根分钟（定位交易日，1 次请求） | `rd.keys("分钟k", code, "*")[-1:].do()` |
  | 日线补 name / pre_close | `rd.get_data(code, start=..., end=..., frequency="1d", fq=None)` |

- 表 `分钟k`，键 `代码:YYYYMMDDHHMMSS`（14 位 int）。字段
  `open / high / low / close / volume / amount / code / date`；
  `volume` 是**股**，`volume_unit: hand` 才 ×100。
- `fq=None` 是硬要求：分钟线一旦复权就和盘口对不上，分时图会整体偏移。
- 日线直接给 `name` 与 `pre_close`，不再自己拿「前一天收盘」去推。
- **无均价字段**：适配器用「当日累计额 / 当日累计量」递推，与东财 `f58` 同口径。

#### 必须预加载（否则分时页一定空）

PySide6 的 shiboken 导入钩子会对每个新模块做 `inspect.unwrap()`，遇到 `stockdb.pyd`
抛 `ValueError: wrapper loop when unwrapping ...`。所以 **Qt 先加载后，`import stock_sdk` 必然失败**。

- 入口 `app/main.py` 在 `import PySide6` 之前调用
  `app.adapters.sdk_bootstrap.preload(config_path)`；成功结果进程级缓存。
- 之后所有取数从缓存拿 `rd`，**不再触发 `stockdb` 的 import**。
- 自己写调试脚本时，也要把 `import stock_sdk` 放在 `import PySide6` 之前。
- 失败分两类：`not_installed`（找不到 `sdk_dir`）、`qt_conflict`（Qt 先加载了）。

### 约束

- 数据是同步落盘的，不是推送流：盘中最新一分钟不等于今天 → 抛
  `DataSourceError(kind="stale")`，不拿昨日冒充今日（`strict_today: true`）。
- 当日无数据时向前回溯最多 `days` 个自然日，取最近一个有数据的交易日。
- `fallback_eastmoney: true` 时由 `FallbackIntradayAdapter` 包装：主源失败立即走东财，
  `fallback_cooldown_sec` 内不再探主源，回落原因留在 `last_reason`。
- 来源标记：东财 `em_minute`、free-stockdb `stockdb_minute`，都写进
  `SamplePoint.quality_flag` 与 `kline_history.quality_flag`；
  查「所有真分钟线」用 `MINUTE_FLAGS`（`repo.load_bars(quality_contains=MINUTE_FLAGS)`）。
- `data_level` 固定 `C`：本地同步数据，复盘可信、实时性取决于同步频率。
- 界面不写死来源：标题与等级取 `MinuteFeed.meta()` 的 `minute_flag / data_level`，
  回落时跟着变；判断「是不是真分钟线」统一用 `charts.is_minute_source(source)`，
  不要再写 `source == "em"`。
- 实机自检：`python scripts/verify_stockdb.py [--symbol ...] [--sdk-dir ...] [--raw]`。

## 断线与降级

- 断线：状态条红色「行情断开 hh:mm:ss」，停止写入 `quotes_cache`，暂停所有依赖实时价的预警，图表停止右移。
- 恢复：补拉缺失区间（K线/分时按缺口区间请求），补齐后在图上以浅色背景标出缺口区间，并在 `alert_events` 中记录 `event_type=gap_recovered`。
- 非交易时段：不订阅、不预警，界面显示「非交易时段」；午间休市 11:30–13:00 冻结刷新手势但保留最后一次快照。
- 停牌：`stock_info.status='suspended'` 时所有实时预警对该股静默，详情页显示停牌起始日。
