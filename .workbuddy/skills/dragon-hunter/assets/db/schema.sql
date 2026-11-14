-- 寻龙盯盘 Dragon Hunter — SQLite schema (single source of truth)
-- 所有表统一携带 created_at / updated_at；时间一律 ISO8601 带时区，交易时间按 Asia/Shanghai 存储。
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------- 基础信息
CREATE TABLE IF NOT EXISTS stock_info (
    symbol          TEXT PRIMARY KEY,              -- 600000.SH / 000001.SZ / 300001.SZ
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    market          TEXT NOT NULL,                 -- SH / SZ / BJ
    board           TEXT,                          -- 主板 / 创业板
    industry_l1     TEXT,
    industry_l2     TEXT,
    list_date       TEXT,
    status          TEXT NOT NULL DEFAULT 'listed' CHECK (status IN ('listed', 'suspended', 'delisted')),
    is_st           INTEGER NOT NULL DEFAULT 0,
    limit_up_price  REAL,
    limit_down_price REAL,
    prev_close      REAL,
    source          TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_stock_info_name ON stock_info (name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_info_code_market ON stock_info (code, market);

-- ---------------------------------------------------------------- 分组与布局
CREATE TABLE IF NOT EXISTS watch_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS watch_group_stocks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES watch_groups (id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    added_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (group_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_wgs_symbol ON watch_group_stocks (symbol);

-- 单股模式 / 多股模式的布局、窗口位置、分屏配置，重启后必须能原样恢复
CREATE TABLE IF NOT EXISTS user_layouts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope       TEXT NOT NULL,                     -- global / group / window
    ref_id      TEXT,                              -- group_id 或 window 名
    payload     TEXT NOT NULL,                     -- JSON
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (scope, ref_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ---------------------------------------------------------------- 行情数据
CREATE TABLE IF NOT EXISTS quotes_cache (
    symbol          TEXT PRIMARY KEY REFERENCES stock_info (symbol) ON DELETE CASCADE,
    price           REAL,
    prev_close      REAL,
    open            REAL,
    high            REAL,
    low             REAL,
    change_amount   REAL,
    change_pct      REAL,
    volume          INTEGER,
    amount          REAL,
    turnover_rate   REAL,
    volume_ratio    REAL,
    bid1_price      REAL,
    ask1_price      REAL,
    limit_up_price  REAL,
    limit_down_price REAL,
    data_level      TEXT NOT NULL DEFAULT 'C',     -- A 实时授权 / B 延时 / C 历史 / D 模拟
    source          TEXT,
    quote_time      TEXT,                          -- 交易所侧行情时间
    received_at     TEXT,                          -- 本地接收时间
    latency_ms      INTEGER,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_quotes_level ON quotes_cache (data_level);

CREATE TABLE IF NOT EXISTS kline_history (
    symbol      TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    period      TEXT NOT NULL,                     -- 1m / 5m / 15m / 30m / 60m / 1d / 1w / 1M
    bar_time    TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL,
    volume      INTEGER, amount REAL,
    turnover_rate REAL,
    data_level  TEXT NOT NULL DEFAULT 'C',
    quality_flag TEXT NOT NULL DEFAULT 'exchange', -- exchange 交易所K线 / sampled 快照采样分时
    source      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (symbol, period, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_kline_period_time ON kline_history (period, bar_time);
CREATE INDEX IF NOT EXISTS idx_kline_quality ON kline_history (quality_flag);

CREATE TABLE IF NOT EXISTS tick_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    trade_time    TEXT NOT NULL,                   -- 毫秒精度 ISO8601
    seq           INTEGER,                         -- 数据源序号，用于同毫秒排序
    price         REAL NOT NULL,
    volume        INTEGER NOT NULL,                -- 股
    amount        REAL NOT NULL,                   -- 元
    direction     TEXT NOT NULL DEFAULT 'unknown' CHECK (direction IN ('buy', 'sell', 'unknown')),
    data_level    TEXT NOT NULL DEFAULT 'C',
    source        TEXT,
    quality_flag  TEXT,                            -- ok / dirty / merged / duplicate_dropped
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (symbol, trade_time, price, volume, direction)
);
CREATE INDEX IF NOT EXISTS idx_tick_symbol_time ON tick_history (symbol, trade_time);

CREATE TABLE IF NOT EXISTS large_order_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    tier          TEXT NOT NULL,                   -- normal / large / super / continuous
    direction     TEXT NOT NULL,                   -- buy / sell / unknown
    price         REAL NOT NULL,
    volume        INTEGER,
    amount        REAL NOT NULL,
    threshold     REAL,                            -- 触发时的用户阈值
    event_time    TEXT NOT NULL,
    price_position TEXT,                           -- relative_high / mid / relative_low
    tag           TEXT,                            -- 买入活跃/卖出活跃/买卖分歧/成交放大/量价背离/资金行为不明确
    data_level    TEXT NOT NULL DEFAULT 'C',
    source        TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_loe_symbol_time ON large_order_events (symbol, event_time);

-- ---------------------------------------------------------------- 板块与龙虎榜
CREATE TABLE IF NOT EXISTS concept_mapping (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    concept_type TEXT NOT NULL,                    -- industry_l1 / industry_l2 / concept / theme / mainline
    concept_name TEXT NOT NULL,
    is_core     INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    fetched_at  TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (symbol, concept_type, concept_name)
);
CREATE INDEX IF NOT EXISTS idx_cm_concept ON concept_mapping (concept_type, concept_name);

CREATE TABLE IF NOT EXISTS dragon_tiger_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL REFERENCES stock_info (symbol) ON DELETE CASCADE,
    trade_date    TEXT NOT NULL,
    reason        TEXT,                            -- 上榜原因
    buy_top5      TEXT,                            -- JSON 数组
    sell_top5     TEXT,                            -- JSON 数组
    net_buy       REAL,
    institution_net REAL,
    post_perf     TEXT,                            -- JSON: 上榜后 1/3/5/10 日表现
    publish_date  TEXT,                            -- 数据披露日期，必须填写
    source        TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (symbol, trade_date, reason)
);
CREATE INDEX IF NOT EXISTS idx_dt_date ON dragon_tiger_records (trade_date);

-- ---------------------------------------------------------------- 预警
CREATE TABLE IF NOT EXISTS alert_rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    category      TEXT NOT NULL,                   -- price / volume / intraday / market
    rule_type     TEXT NOT NULL,                   -- limit_up_approach / break_ma5 / volume_spike ...
    scope         TEXT NOT NULL DEFAULT 'symbol',  -- symbol / group / market
    symbols       TEXT,                            -- JSON 数组，空表示按 group
    group_id      INTEGER REFERENCES watch_groups (id) ON DELETE CASCADE,
    params        TEXT NOT NULL DEFAULT '{}',      -- JSON，阈值等
    level         TEXT NOT NULL DEFAULT 'P3' CHECK (level IN ('P1', 'P2', 'P3', 'P4')),
    cooldown_sec  INTEGER NOT NULL DEFAULT 300,
    notify        TEXT NOT NULL DEFAULT '["popup","sound","log"]',
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS alert_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id       INTEGER REFERENCES alert_rules (id) ON DELETE SET NULL,
    symbol        TEXT NOT NULL,
    name          TEXT,
    event_type    TEXT NOT NULL,
    trigger_price REAL,
    trigger_time  TEXT NOT NULL,
    condition     TEXT,                            -- 触发条件快照
    volume        INTEGER,
    amount        REAL,
    data_level    TEXT NOT NULL DEFAULT 'C',
    source        TEXT,
    level         TEXT NOT NULL DEFAULT 'P3',
    handled       INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ae_time ON alert_events (trigger_time DESC);
CREATE INDEX IF NOT EXISTS idx_ae_symbol ON alert_events (symbol, trigger_time DESC);

-- ---------------------------------------------------------------- 复盘与桥接
CREATE TABLE IF NOT EXISTS review_notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT REFERENCES stock_info (symbol) ON DELETE CASCADE,
    trade_date  TEXT NOT NULL,
    note_type   TEXT NOT NULL DEFAULT 'observation', -- observation / summary / plan
    content     TEXT NOT NULL,
    tags        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rn_date ON review_notes (trade_date DESC);

CREATE TABLE IF NOT EXISTS tdx_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL DEFAULT 'tongdaxin',
    event       TEXT NOT NULL,                     -- watchlist_update / selection_result / formula_signal / current_symbol
    channel     TEXT,                              -- csv / json_watch / http / ws / dde
    group_name  TEXT,
    raw_payload TEXT NOT NULL,
    symbol_cnt  INTEGER NOT NULL DEFAULT 0,
    new_cnt     INTEGER NOT NULL DEFAULT 0,
    event_time  TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_te_time ON tdx_events (event_time DESC);

CREATE TABLE IF NOT EXISTS data_source_status (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    data_level    TEXT NOT NULL,
    connected     INTEGER NOT NULL DEFAULT 0,
    latency_ms    INTEGER,
    data_timestamp TEXT,
    last_error    TEXT,
    checked_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (source, data_level)
);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
