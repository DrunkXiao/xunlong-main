---
name: dragon-hunter
description: 寻龙盯盘（Dragon Hunter）A股短线交易工作台的规格与实现指南。当用户要求开发、搭建、迭代、评审或排查该桌面行情终端时使用，覆盖单股精盯与多股对比布局、分时与逐笔成交联动、大单监控与资金行为标签、概念板块联动、龙虎榜标记、通达信桥接导入、本地分组记忆、智能预警引擎以及盘前/盘中/收盘复盘流程。触发词：寻龙盯盘、盯盘终端、龙头战法工作台、单股精盯、多股对比、逐笔大单联动、通达信桥接、盘前复盘、盘中异动预警、PySide6 行情终端。
agent_created: true
---

# 寻龙盯盘 Dragon Hunter

## 角色

以 A股短线交易终端产品架构师 + 量化行情工程师 + 龙头战法研究员的身份交付一套 Windows 桌面行情工作台。服务对象是短线交易者、龙头战法选手、打板选手、趋势交易者与超短线复盘研究者。

产出必须是可运行的源码工程（含依赖清单、建库脚本、配置模板、启动入口、测试），而不是 UI 设计图或伪代码。工具定位为行情分析与决策辅助，不代替用户下单。

## 何时启用

- 从零搭建「寻龙盯盘」或同类 A股桌面盯盘终端
- 给现有终端增加模块：多股对比、逐笔联动、大单引擎、概念板块、龙虎榜、强势回调选股、涨停四阶段选股、通达信桥接（含「通达信联动」外发按钮）、预警、复盘
- 评审/排查上述模块的数据真实性、时间戳、断线处理、重复触发、分组持久化问题
- 设计行情数据适配器与数据源分级

不适用：单纯选股策略计算、盘后回测指标（走 `a-share-limitup-hunter` / `ashare-strong-pullback` 等选股类技能）。
例外：这两个技能的规则引擎都已移植进本项目 —— `ashare-strong-pullback` →
`app/services/pullback.py` + 顶栏「强势回调」视图；`a-share-limitup-hunter` →
`app/services/limitup.py` + `limitup_pool.py` + 顶栏「涨停四阶」视图。
要改口径请改这里，不要再跑外部技能，否则两套口径会互相矛盾。

## 十条铁律

违反任意一条即为交付失败，优先级高于任何功能进度。

1. **数据真实可追溯**：每一条价格、成交量、资金数据都必须带 `source` 与 `时间戳`。数据缺失输出 `N/A` 并显式标记「数据缺失」，禁止编造或按历史均值填补。
2. **行情等级显式区分**：实时 / 延时 / 历史 / 模拟四级（A/B/C/D）必须落到字段 `data_level`，并在界面持续可见。采样推演数据一律标 `B` + `quality_flag='sampled'`。
3. **不得冒充能力**：数据源没有的能力必须显式声明为不支持，并在界面说明原因与替代方案。当前默认源 tqcenter **没有逐笔成交、没有分钟K、没有龙虎榜**——这三项不得用日线插值、快照采样或估算数据顶替后仍称原名。
4. **不推断资金身份**：大单只描述成交金额、方向、价格位置与连续性。文案只用「买入活跃 / 卖出活跃 / 买卖分歧 / 成交放大 / 量价背离 / 资金行为不明确」，禁止出现「主力建仓」「机构出货」「庄家」等身份结论。
5. **A股配色**：红涨绿跌。上涨 `#F04438` 系，下跌 `#12B76A` 系，警告黄，信息蓝，未知灰。禁止欧美红跌绿涨。
6. **预警只提示**：不生成无条件买卖指令，不出现「必买」「稳赚」类表述。同一事件必须走冷却去重。
7. **状态必须落盘**：分组、分组内排序、单股/多股布局、图表周期、预警条件、窗口位置全部写 SQLite，重启原样恢复。禁止只存内存或只存 JSON 临时文件。
8. **数据源可替换**：业务层只依赖 `MarketDataAdapter` / `TradeDataAdapter` / `IndustryDataAdapter` / `DragonTigerAdapter` / `DataSourceManager` 协议，禁止在 UI 或服务层硬编码某家供应商 SDK。
9. **异常即降级**：断线、延迟超阈值、时间戳滞后于交易时钟超过阈值时，界面显示状态条并暂停依赖该数据的信号，恢复后补标记缺口。调用数据源前先本地校验代码——tqcenter 收到非法代码会 `tq.close()` 关掉整条连接。
10. **双向联动**：分时图 ⇄ 成交列表 ⇄ 大单记录 ⇄ K线事件标记，点击任一侧其余同步定位；多股模式下十字光标与时间轴共享。

## 技术栈

默认 Python 3.11+ / PySide6 / FastAPI / WebSocket / Lightweight Charts（或等价成熟金融图表组件）/ Pandas / NumPy / SQLite / SQLAlchemy / Pydantic。组件均可替换，但先交付稳定最小可运行版本（MVR），再逐阶段叠加。

## 数据源：tqcenter（默认）

通达信 TdxQuant 本地数据服务，目录 `D:\new_tdx64\PYPlugins\user`（同级需 `PYPlugins\TPythClient.dll`）。
适配器实现见 `assets/adapters/tqcenter_adapter.py`，全部实测细节见 `references/tqcenter.md`。

```python
import sys; sys.path.insert(0, r"D:\new_tdx64\PYPlugins\user")
from tqcenter import tq
tq.initialize(r"D:\new_tdx64\PYPlugins\user\_dragon_hunter_<pid>.py")
```

| 能力 | 状态 | 结论 |
|---|---|---|
| 日/周/月 K | 可用 | Volume 股、**Amount 万元** |
| 实时快照 + 五档 | 可用 | ~11ms/只，Volume/内外盘 **手**，Amount **万元** |
| 行业 | 可用 | 必须显式请求 `rs_hyname`，空 field_list 不返回 |
| 板块 / 成分股 | 部分 | 569 个板块；个别板块成分异常，缺失就标灰 |
| 分钟 K（1/5/15/30/60m） | **不可用** | 返回空；`refresh_kline` 也补不出 |
| 逐笔成交 | **不可用** | `period='tick'` 报 `error=-5`；`subscribe_quote` 是空壳 |
| 龙虎榜 | **不可用** | 改 CSV 导入，必须带披露日与来源 |
| 订阅 | 上限 100 只 | 回调只有 `{"Code":...}`，需反手拉快照 |

因此：分时线与分钟级「大单」只能由快照轮询差分得到，标 `B` 级 + `sampled`，界面写「采样分时」。
逐笔面板必须置灰并说明不支持，不得渲染空壳或伪造流水。

## 目标工程结构

```
dragon_hunter/
├── app/
│   ├── main.py                 # 启动入口：拉起后端 + 桌面窗口
│   ├── config.py               # 配置加载（config.yaml → Pydantic Settings）
│   ├── db/                     # session.py / models.py / schema.sql
│   ├── adapters/               # base.py(协议) / mock.py(D级) / <vendor>.py
│   ├── services/               # quote / kline / tick / large_order / alert / tdx_bridge
│   ├── api/                    # FastAPI 路由 + WebSocket 推送
│   └── ui/                     # theme.py / main_window.py / widgets/
├── scripts/                    # init_db.py / run.py
├── tests/
├── requirements.txt
├── config.example.yaml
└── README.md
```

## 构建流程

按阶段推进，每阶段结束必须给出「可运行命令 + 实际运行结果 + 未完成接口清单」。

| 阶段 | 范围 | 验收标准 |
|---|---|---|
| 1 MVR | 股票搜索、单股模式、多股模式（2/4/6/9/12）、日K与采样分时、自选分组、本地记忆、基础价格预警 | 冷启动加载 5000+ 代码，切组不丢排序，关闭重开恢复布局，触发一条价格预警并落库，真实快照价格与 tqcenter 一致 |
| 2 资金行为 | 采样成交列表、大单四档（区间口径）、采样⇄大单⇄K线联动、多股共享时间轴、成交额与量能预警 | 采样点间隔与配置一致；区间量额等于两次快照差分；逐笔面板显示不支持而非空白；同值抑制有效 |
| 3 信息联动 | 行业与板块成分、板块内强弱排名、龙虎榜 CSV 导入、通达信桥接、公式信号导入 | 概念标签打开成分股对比窗；板块成分缺失时标灰不补假数据；桥接重复推送去重并记录 `tdx_events` |
| 4 智能复盘 | 盘中异动分析、龙头地位观察、个股强弱对比、历史事件检索、AI 观察报告 | 复盘报告每个结论都附数据依据与不确定项，采样数据标注「非逐笔」 |
| 5 稳定性 | 多数据源切换、断线恢复、行情缓存、数据库优化、大池轮询压测、预警延迟测试 | 数据服务断开 30s 内显示断线并停止信号；恢复后无重复订阅；100 只以上改分组轮询 UI 不卡死 |

细节要求分列在以下参考文件，按需加载，不要一次性全部读入：

| 文件 | 内容 |
|---|---|
| `references/tqcenter.md` | tqcenter 实测能力矩阵、单位换算、性能、踩坑清单、模块落地映射（**接数据源前必读**） |
| `references/modules.md` | 模块 A–K 的字段清单、交互规则、阈值与文案约束 |
| `references/data-adapters.md` | 五类适配器签名、数据源分级、时间戳与去重规则、通达信桥接协议 |
| `references/alert-rules.md` | 四类预警矩阵、事件等级、去重冷却、事件结构 |
| `references/ui-spec.md` | 暗色主题色板、五区布局、面板与图表规范、状态条 |
| `references/workflows.md` | 盘前/盘中/收盘三段流程、交易时段与休市处理 |
| `references/review-report.md` | AI 观察报告字段模板与结论口径 |

## 预警引擎数据流

数据进入 → 指标计算 → 条件匹配 → 事件生成 → 去重（规则冷却 + 同值抑制）→ 通知（弹窗/声音/内部消息/日志）→ 本地记录。

事件必须带：代码、名称、事件类型、触发价格、触发时间、触发条件、对应成交量、数据来源、事件等级（P1 重大异动 / P2 重要突破 / P3 普通观察 / P4 提示）、是否已处理。

## 一键脚手架

生成新工程骨架（目录、依赖、配置模板、建库脚本、可运行的 FastAPI + PySide6 最小窗口、tqcenter 适配器、冒烟测试）：

```bash
python scripts/scaffold_project.py <目标目录> [--force]
```

脚本会复制 `assets/db/schema.sql` 作为唯一 DDL 来源并执行建库，复制 `assets/adapters/tqcenter_adapter.py` 到 `app/adapters/`，默认装配 tqcenter。

实测：裸环境（无 PySide6/FastAPI 也可）生成骨架 → 17 张业务表 → 9 项冒烟测试全通过；接真实 tqcenter 后
`python -m app.adapters.tqcenter_adapter 600000.SH` 可打印能力矩阵、行情、日K、盘口、行业，并把分钟线报为
`unsupported`。单股全字段约 31ms，12 只约 115~373ms。

## 交付前自检

- [ ] `python -m pytest` 与 `python scripts/init_db.py` 均实际执行通过
- [ ] `python -m app.adapters.tqcenter_adapter 600000.SH` 能打出真实行情，且 5m 报 unsupported
- [ ] 所有行情读取路径都能回答：数据来自哪个源、什么等级、什么时间戳
- [ ] 逐笔/分钟K/龙虎榜三处在界面上明确标注「数据源不支持」，而不是空表或假数据
- [ ] 采样分时的间隔、点数与轮询配置一致，界面标「采样」
- [ ] 非法代码（`300001.SH`、`60000`、已退市）在进入数据源前被拦下
- [ ] 断网、数据服务未启动、停牌、午间休市、非交易日五种情况都有界面状态与日志
- [ ] 分组增删改、拖拽排序、批量导入、分组复制、重启恢复全部可用
- [ ] 预警冷却生效，连续触发不刷屏
- [ ] 多股模式下十字光标、缩放、滚动同步
- [ ] 报告中不含任何主力/机构身份推断，采样数据标注「非逐笔」
- [ ] 未完成接口在 README 中列明，不隐瞒

## 资源

- `scripts/scaffold_project.py` — 生成可运行工程骨架
- `assets/adapters/tqcenter_adapter.py` — tqcenter 适配器（含 SnapshotSampler 采样引擎，可独立自检）
- `assets/db/schema.sql` — 全库 DDL（改表只改这里）
- `assets/templates/requirements.txt` — 依赖清单
- `assets/templates/config.example.yaml` — 配置模板（默认 tqcenter + 其 user_dir）
- `assets/ui/theme.css` — 暗色交易主题 QSS
- `references/tqcenter.md` — 数据源实测手册
