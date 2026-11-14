# 预警规则与引擎

## 规则矩阵

`rule_type` 为稳定标识，落库后不可随意改名（改名需迁移）。

### 价格类 `category=price`

| rule_type | 触发条件 | 默认参数 | 默认等级 |
|---|---|---|---|
| `break_prev_high` | 最新价 > 昨日最高 | — | P2 |
| `break_platform` | 收盘/最新价突破近 N 日平台高点 | `lookback=20` | P2 |
| `break_prior_high` | 突破前高（近 120 日最高） | `lookback=120` | P1 |
| `near_limit_up` | 距涨停价 ≤ x% | `pct=1.0` | P2 |
| `near_limit_down` | 距跌停价 ≤ x% | `pct=1.0` | P2 |
| `break_ma` | 跌破/上穿 MA(n) | `n=5/10/20`, `direction=down` | P3 |

### 成交类 `category=volume`

| rule_type | 触发条件 | 默认参数 |
|---|---|---|
| `amount_spike` | 分钟成交额 > 近 20 分钟均值 × k | `k=3` |
| `volume_ratio` | 量比 ≥ x | `x=2.5` |
| `turnover_rate` | 换手率 ≥ x% | `x=5` |
| `large_order_single` | 单笔成交额 ≥ 阈值 | `amount=1_000_000` |
| `large_order_streak` | 连续 N 笔同向且累计达标 | `n=3, amount=2_000_000` |

### 分时类 `category=intraday`

| rule_type | 触发条件 | 默认参数 |
|---|---|---|
| `surge` | 3 分钟内涨幅 ≥ x% | `minutes=3, pct=1.5` |
| `plunge` | 3 分钟内跌幅 ≥ x% | `minutes=3, pct=1.5` |
| `cross_vwap` | 上穿/下穿均价线 | `direction=up` |
| `pullback_vwap` | 回踩均价线后回升（触及 ±0.3% 后收于上方） | `band=0.003` |
| `break_intraday_high` | 放量突破当日分时高点 | `vol_ratio=1.5` |
| `limit_up_open` | 涨停炸板（封板后开板） | — |
| `limit_up_reseal` | 炸板后重新封板 | — |

### 市场类 `category=market`

| rule_type | 触发条件 |
|---|---|
| `sector_move` | 概念板块涨幅 ≥ x% 且成交额放大 |
| `leader_plunge` | 龙头股 5 分钟跌幅 ≥ x% |
| `sector_limit_count_change` | 板块涨停家数 5 分钟内变化 ≥ n |
| `core_volume_align` | 板块内 ≥3 只核心股同时放量 |
| `leader_sector_divergence` | 龙头与板块指数方向相反且幅度差 ≥ x% |

## 事件等级

- **P1 重大异动**：涨停炸板、龙头跳水、突破前高、板块集体异动 → 弹窗 + 声音 + 高亮日志
- **P2 重要突破**：破平台、接近涨跌停、大单连续 → 弹窗 + 声音
- **P3 普通观察**：破均线、量比达标、换手达标 → 内部消息 + 日志
- **P4 提示信息**：数据源切换、缺口补齐、桥接导入结果 → 仅日志

## 引擎流程

```
quote/tick 到达
  → 指标增量更新（MA/量比/换手/分钟聚合）
  → 规则匹配（按 scope 过滤：symbol / group / market）
  → 生成 AlertEvent 草稿
  → 去重（见下）
  → 通知分发（popup / sound / log / websocket）
  → 写 alert_events
```

## 去重与冷却

两道闸门，缺一不可：

1. **规则冷却**：同 `rule_id + symbol + event_type` 在 `cooldown_sec` 内只产出一次。默认 300s，P1 类可降到 60s。
2. **同值抑制**：价格类规则若本次触发价与上次触发价差异 < `tick_size × 2`，视为同一事件延续，不重复通知，只更新原事件的时间与次数（`alert_events.updated_at` + 计数）。

附加约束：

- 同一 `symbol` 每分钟通知总数上限 5 条，超出合并为一条「该股 1 分钟内 N 条异动」摘要。
- 非交易时段、停牌、数据等级为 D 时不触发。
- 数据延迟超阈值期间的触发标记 `data_level` 并降级一级处理（P2→P3）。
- 规则增删改必须即时生效，无需重启；修改后清空该规则的冷却状态。

## 事件结构

```json
{
  "rule_id": 12,
  "symbol": "600000.SH",
  "name": "示例股票",
  "event_type": "near_limit_up",
  "trigger_price": 11.85,
  "trigger_time": "2026-11-08T10:31:22.417+08:00",
  "condition": "distance_to_limit_up=0.85%",
  "volume": 128400,
  "amount": 15183000.0,
  "data_level": "A",
  "source": "vendor_x",
  "level": "P2",
  "handled": 0
}
```

`condition` 必须记录触发瞬间的判定输入，便于事后复核。禁止只写「满足条件」。

## 文案约束

通知文案模板：`{名称} {事件中文描述}（{触发价}，{时间}）`，例如「示例股票 距涨停价 0.85%（11.85，10:31:22）」。

禁止出现：买卖建议、目标价、「主力进场」、「必涨」、「建议买入」。
