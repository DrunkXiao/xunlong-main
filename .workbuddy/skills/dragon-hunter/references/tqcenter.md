# tqcenter 数据源（通达信 TdxQuant）

实现：`app/adapters/tqcenter_adapter.py`（技能内为 `assets/adapters/tqcenter_adapter.py`）
环境：`tqcenter.py` v1.0.3，目录 `D:\new_tdx64\PYPlugins\user`，同级 `PYPlugins\TPythClient.dll`
本文数字均为本机实测（2026-11-08，Windows / Python 3.13）。

## 1. 连接

```python
import sys; sys.path.insert(0, r"D:\new_tdx64\PYPlugins\user")
from tqcenter import tq
tq.initialize(r"D:\new_tdx64\PYPlugins\user\_dragon_hunter_12345.py")  # 参数被当作"策略名"
```

| 现象 | 原因 | 处理 |
|---|---|---|
| `TQ数据接口初始化失败或已有同名策略运行` | 上次进程被 kill，同名注册未释放 | 策略名带 pid：`_dragon_hunter_<pid>.py` |
| 退出时刷 `TQ数据连接已关闭` | tqcenter 自己注册了 atexit | 初始化后注册自己的 atexit 先把 `_initialized` 置 False |
| 满屏 `警告：请求字段'X'在结果中不存在` | 内部用 print 报错，不是异常 | 所有调用包 `contextlib.redirect_stdout` |
| 一次调用后后续全部失败 | 代码格式非法时 tqcenter 内部执行 `tq.close()`，把整条连接关了 | 所有入口先过本地 `_valid_symbol()` |

不需要通达信窗口在前台，但客户端必须已安装、本地数据已下载。首次连接约 1.9s，之后走本地服务。

## 2. 能力矩阵（实测）

| 能力 | 状态 | 接口 | 备注 |
|---|---|---|---|
| 日 / 周 / 月 K | 可用 | `get_market_data(period='1d'/'1w'/'1mon')` | 首次约 5.9s（冷），之后 ~14ms/只 |
| 分钟 K（1/5/15/30/60m） | **不可用** | `get_market_data(period='1m'...)` | 返回空 dict；`refresh_kline(period='1m')` 返回成功但仍为空 |
| 逐笔成交 | **不可用** | `period='tick'` | 返回 `{'error': -5, 'msg': '周期格式错误'}`；`subscribe_quote` 源码注释「暂无实际功能」 |
| 实时快照 | 可用 | `get_market_snapshot(code)` | ~11ms/只，26 字段，含五档与内外盘 |
| 行情更新通知 | 可用（弱） | `subscribe_hq(list, cb)` | 回调仅 `{"Code":...,"ErrorId":"0"}`，无行情字段；**单次上限 100 只** |
| 基础信息 | 可用 | `get_stock_info(code, field_list)` | 行业字段必须显式请求：`rs_hyname` / `rs_hycode_sim` |
| 更多指标 | 可用 | `get_more_info(code)` | 涨跌停价、换手率、量比、市值、多周期涨幅 |
| 板块列表 | 可用 | `get_sector_list(1)` | 569 个 |
| 板块成分股 | 部分 | `get_stock_list_in_sector(code)` | 880301 返回 32 只正常；880081 只返回 2 只 ETF（部分板块数据缺失） |
| 自选股板块 | **不可用** | `get_user_sector()` / `get_stock_list_in_sector("1B")` | 能列板块名（1B/2B/3B…），取成分报 `server return none` |
| 龙虎榜 | **不可用** | — | 需 CSV 导入并标注来源与披露日 |
| 交易日历 | 可用 | `get_trading_dates('SH', '20261101','20261110')` | 返回 `['20261102', ...]` |

## 3. 单位换算（错一次全套数据都错）

| 来源 | 字段 | 原始单位 | 换算 |
|---|---|---|---|
| get_market_data | Volume | 股 | 直接用 |
| get_market_data | Amount | **万元** | ×10000 → 元 |
| get_market_snapshot | Volume / NowVol / Inside / Outside | **手** | ×100 → 股 |
| get_market_snapshot | Amount | **万元** | ×10000 → 元 |
| get_stock_info | ActiveCapital | **万股** | ×10000 → 股（换手率分母） |
| get_more_info | fHSL | % | 直接用 |
| get_more_info | Zsz / Ltsz | 亿元 | ×1e8 → 元 |
| get_more_info | ZTPrice / DTPrice | 元 | 直接用（涨跌停价） |

`fill_data=False` 时停牌日为 NaN，必须跳过；把 NaN 当 0 会把"停牌"显示成"零成交"。

## 4. 关键字段

`get_market_snapshot`：
`Now` 现价、`LastClose` 昨收、`Open/Max/Min`、`Volume` 累计量(手)、`Amount` 累计额(万元)、
`Average` 分时均价、`Inside/Outside` 内盘/外盘(手)、`Buyp/Buyv` 买一~买五、`Sellp/Sellv` 卖一~卖五、
`Before5MinNow` 5 分钟前价、`TickDiff`、`InOutFlag`。

`get_more_info`：
`ZTPrice/DTPrice` 涨跌停价、`fHSL` 换手率、`fLianB` 量比、`ZAF` 涨幅、`ZAFPre5/20` 多周期涨幅、
`Zsz/Ltsz` 总/流通市值、`Wtb` 委比、`Zjl` 资金流、`BCancel/SCancel` 买撤/卖撤、
`L2TicNum/L2OrderNum` L2 笔数/委托数（需 L2 权限）、`EverZTCount`、`HqDate` 行情日期。

`get_stock_info`（显式请求）：`Name`（含全角空格需清洗）、`Unit` 每手股数、`MinPrice` 最小变动价位、
`ActiveCapital` 流通股本(万股)、`IsSTGP`、`IsQuitGP`、`DelayMin`（0 表示无延时）、`rs_hyname`、`rs_hycode_sim`。

## 5. 性能实测与轮询策略

| 操作 | 耗时 |
|---|---|
| 首次连接 | ~1.9s |
| `get_stock_list`（5224 只，主板+创业板） | 1.1~1.3s |
| `get_market_snapshot` 单只 | ~9~11ms |
| `get_more_info` 单只 | ~27ms |
| `get_quote` 全字段（快照+信息+更多） | ~31ms/只，12 只 373ms |
| 同上，`more` 命中 2s 缓存 | 12 只 115ms |

结论：单股精盯 3s 轮询毫无压力；多股 12 格约 115~370ms/轮；**100 只以上必须分组轮询**
（可见分组优先，非可见分组 15s 一轮），不要指望一次拉全池。

## 6. 对此项目的落地映射（必须遵守）

| 模块 | 原设计 | tqcenter 下的实现 |
|---|---|---|
| A 单股 | K线 + 分时 + 五档 | 日/周/月K 直连；分时用快照采样；五档直读 `Buyp/Buyv` |
| B 多股 | 2/4/6/9/12 格 | 12 格 115ms 可行；每格独立周期，十字光标共享时间轴 |
| C 逐笔联动 | 逐笔列表 | **逐笔面板置灰并说明不支持**；改展示「采样成交」：每 N 秒一行（区间量、区间额、方向推演） |
| D 大单 | 单笔金额四档 | 降级为**区间累计成交额**四档，标 `quality_flag='sampled'`，文案写「分钟内累计」而非「单笔」 |
| E 概念板块 | 概念/主线 | 行业用 `rs_hyname`；板块用 `get_sector_list` + 成分；成分缺失标灰，不补假数据 |
| F 龙虎榜 | 接口 | **CSV 导入**，必须带 `publish_date` 与 `source` |
| G 通达信桥接 | 双向 | tqcenter 只读，写入分组仍需 CSV/HTTP/文件监听通道 |
| H 分组记忆 | 本地 | 不依赖数据源，直接 SQLite |
| I 预警 | 全量 | 价格/量能/盘口类可用；逐笔类（连续主动买）降级为采样区间 |

## 7. 采样分时与"分钟大单"的正确说法

`SnapshotSampler` 用相邻两次快照差分：

```
区间成交量 = (Volume_t - Volume_{t-1}) × 100               # 股
区间成交额 = (Amount_t - Amount_{t-1}) × 10000             # 元
区间均价   = 区间成交额 / 区间成交量
方向推演   = 外盘增量 > 内盘增量×1.2 → buy；内盘反向 → sell；否则 unknown
```

必须写在界面与报告里的话：

- 「采样分时」不是交易所分时，间隔等于轮询间隔（默认 3s），两点之间的价格路径未知。
- 「区间成交额」不是单笔成交额，一笔 500 万可能由几十笔拆单构成，反之亦然。
- 方向是内外盘增量推演，主动买卖的真实成交方向不可得。
- 休市 / 停牌 / 数据源未推送时，差分为 0 表示「无新增数据」，不代表「无成交」——必须另用时间戳判断。

禁止表述：主力买入、机构建仓、大单扫货、封单是机构行为。
