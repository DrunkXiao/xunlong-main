"""强势回调扫描结果的单文件 HTML 报告（内嵌样式、无外链、无 JS，可双击打开或打印）。

为什么不复用界面：截图和表格复制到聊天窗口都会丢字段；报告是唯一能整份交出去、
也能留档回溯「当时是什么数据、什么参数、为什么没选出票」的载体。

报告只陈述事实：数据源、数据日、参数、每只候选的价位与仓位建议、未通过原因统计；
不写收益承诺，也不把任何结论说成必然。
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from app.services.pullback import CHECKS, SCORE_PARTS, SIGNAL_LABEL, ScanResult

_CSS = """
* { box-sizing: border-box; }
body { margin: 0; padding: 28px 32px 48px; background: #F7F8FA; color: #1B1F27;
       font: 13px/1.6 "Microsoft YaHei", "Segoe UI", sans-serif; }
h1 { margin: 0 0 4px; font-size: 21px; }
h2 { margin: 26px 0 8px; font-size: 15px; border-left: 3px solid #3D8BFD; padding-left: 8px; }
.sub { color: #5C6673; font-size: 12px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border: 1px solid #E2E6EC; font-size: 12px; }
th, td { padding: 5px 8px; border-bottom: 1px solid #EDF0F4; text-align: left; }
th { background: #F0F3F7; font-weight: 600; color: #46505F; white-space: nowrap; }
td.num { text-align: right; font-family: "Consolas", monospace; }
tr:hover td { background: #FAFBFD; }
.tag { display: inline-block; padding: 0 6px; border-radius: 3px; font-size: 11px; }
.buy { background: #FDECEA; color: #C0341B; border: 1px solid #F5C6BE; }
.watch { background: #FFF6E5; color: #9A6A00; border: 1px solid #F2DDAE; }
.block { background: #F1F3F6; color: #5C6673; border: 1px solid #E2E6EC; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; }
.card { flex: 1 1 320px; background: #fff; border: 1px solid #E2E6EC; border-radius: 6px;
        padding: 12px 14px; }
.card h3 { margin: 0 0 6px; font-size: 14px; }
.kv { display: grid; grid-template-columns: 88px 1fr; gap: 2px 8px; font-size: 12px; }
.kv b { color: #46505F; font-weight: 600; }
.ok { color: #0B7A45; }
.no { color: #B3261E; }
.note { background: #fff; border: 1px solid #E2E6EC; border-left: 3px solid #F5B544;
        padding: 10px 14px; font-size: 12px; color: #46505F; }
footer { margin-top: 28px; color: #7A828E; font-size: 11px; }
"""


def _num(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "--"


def _signal_tag(signal: str) -> str:
    cls = {"BUY-A": "buy", "WATCH": "watch"}.get(signal, "block")
    short = {"BUY-A": "确认", "WATCH": "观察"}.get(signal, "剔除")
    return f'<span class="tag {cls}" title="{escape(SIGNAL_LABEL.get(signal, ""))}">{short}</span>'


def _candidate_table(result: ScanResult, limit: int) -> str:
    rows = []
    for idx, pick in enumerate(result.picks[:limit], 1):
        rows.append(
            "<tr>"
            f"<td class='num'>{idx}</td>"
            f"<td>{escape(pick.code)}</td>"
            f"<td>{escape(pick.name)}</td>"
            f"<td>{escape(pick.industry or '--')}</td>"
            f"<td class='num'>{pick.score}</td>"
            f"<td>{_signal_tag(pick.signal)}</td>"
            f"<td class='num'>{_num(pick.close)}</td>"
            f"<td class='num'>{_num(pick.ma10)}</td>"
            f"<td class='num'>{_num(pick.ma20)}</td>"
            f"<td class='num'>{_num(pick.volume_ratio, 2)}</td>"
            f"<td>{escape(pick.limitup_date or '--')}</td>"
            f"<td>{escape(pick.pattern or '--')}</td>"
            f"<td class='num'>{_num(pick.stop)}</td>"
            f"<td class='num'>{_num(pick.target)}</td>"
            "</tr>")
    if not rows:
        return "<p class='sub'>本轮没有符合条件的候选，等待。</p>"
    return (
        "<table><thead><tr>"
        "<th>#</th><th>代码</th><th>名称</th><th>行业</th><th>评分</th><th>信号</th>"
        "<th>收盘</th><th>MA10</th><th>MA20</th><th>缩量比</th><th>最近涨停</th>"
        "<th>止跌形态</th><th>止损</th><th>2.5R目标</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _plan_cards(result: ScanResult, limit: int = 8) -> str:
    cards = []
    for pick in result.picks[:limit]:
        if pick.signal == "BLOCK":
            continue
        checks = "".join(
            f"<span class='{'ok' if pick.checks.get(key) else 'no'}'>"
            f"{'✓' if pick.checks.get(key) else '✗'} {escape(label)}</span> "
            for key, label in CHECKS)
        score = " · ".join(
            f"{escape(label)} {pick.score_parts.get(key, 0)}" for key, label, _ in SCORE_PARTS)
        cards.append(
            "<div class='card'>"
            f"<h3>{escape(pick.name)} {escape(pick.code)} {_signal_tag(pick.signal)}</h3>"
            "<div class='kv'>"
            f"<b>行业</b><span>{escape(pick.industry or '--')}</span>"
            f"<b>收盘价</b><span>{_num(pick.close)}</span>"
            f"<b>买入区间</b><span>{_num(pick.buy_low)} – {_num(pick.buy_high)}</span>"
            f"<b>止损价</b><span>{_num(pick.stop)}（回调低点 {_num(pick.pullback_low)} 下方）</span>"
            f"<b>2.5R 目标</b><span>{_num(pick.target)}</span>"
            f"<b>建议股数</b><span>{pick.shares} 股 · 风险 {_num(pick.risk_amount, 0)} 元</span>"
            f"<b>市值占用</b><span>{_num(pick.position_value, 0)} 元</span>"
            f"<b>评分</b><span>{pick.score}/100 · {escape(score)}</span>"
            f"<b>硬性条件</b><span>{checks}</span>"
            "</div></div>")
    if not cards:
        return "<p class='sub'>本轮没有确认或观察级标的。</p>"
    return "<div class='cards'>" + "".join(cards) + "</div>"


def _reject_table(result: ScanResult) -> str:
    evaluated = max(result.evaluated or len(result.picks), 1)
    rows = []
    for key, label in CHECKS:
        count = int((result.reject or {}).get(key, 0))
        rows.append(
            f"<tr><td>{escape(label)}</td><td class='num'>{count}</td>"
            f"<td class='num'>{count / evaluated * 100:.1f}%</td></tr>")
    return ("<table><thead><tr><th>未通过的硬性条件</th><th>未通过只数</th><th>占比</th></tr></thead>"
            "<tbody>" + "".join(rows) + "</tbody></table>")


def _market_block(result: ScanResult) -> str:
    m = result.market or {}
    parts = [
        f"样本 {m.get('total', 0)} 只",
        f"上涨 {m.get('up', 0)} / 下跌 {m.get('down', 0)} / 平盘 {m.get('flat', 0)}",
        f"平均涨跌 {_num(m.get('avg_pct'), 2, '%')}",
        f"当日涨停 {m.get('limit_up', 0)} 只",
        f"收盘在 MA20 上方 {(m.get('above_ratio') or 0) * 100:.1f}%",
    ]
    return "<p>" + " · ".join(escape(p) for p in parts) + "</p>"


def render(result: ScanResult, config: dict | None = None) -> str:
    """生成完整 HTML 文本。`config` 只用来显示账户权益口径。"""
    cfg = (config or {}).get("pullback") or {}
    top_n = int(cfg.get("report_top_n", 60) or 60)
    equity = float((result.params or {}).get("账户权益") or cfg.get("account_equity") or 0)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    params = " · ".join(f"{escape(k)} {escape(str(v))}" for k, v in (result.params or {}).items())
    notes = "".join(f"<div class='note'>{escape(n)}</div>" for n in (result.notes or []))
    errors = result.errors or []
    error_line = f"<p class='sub'>异常 {len(errors)} 只（前 5 条）：" + escape(
        "；".join(e.get("symbol", "") + " " + e.get("error", "") for e in errors[:5])) + "</p>" \
        if errors else ""

    head = (
        "<h1>A股强势回调低吸 · 扫描报告</h1>"
        f"<div class='sub'>生成 {generated} · 数据源 {escape(result.source_label or '--')} · "
        f"数据日 <b>{escape(result.data_date or '--')}</b>"
        f"（距今 {result.stale_days if result.stale_days is not None else '--'} 个自然日）· "
        f"等级 C（日线收盘，非实时）· 扫描于 {escape(result.scanned_at or '--')}</div>"
        f"<div class='sub'>股票池 {result.universe} 只 · 成功评估 {result.scanned} 只 · "
        f"出结论 {result.evaluated} 只 · 本次列出 {len(result.picks)} 只 · "
        f"耗时 {result.elapsed:.1f}s · 账户权益 {equity:,.0f} 元</div>"
        f"<div class='sub'>参数：{params}</div>")

    allowed = ("<div class='note'>数据未过期，确认信号可用。</div>" if result.allow_buy else
               "<div class='note'>数据已过期或本次被中断：按规则不生成确认信号，全部标为观察。</div>")

    body = (
        head
        + "<h2>市场环境</h2>" + _market_block(result)
        + "<h2>交易计划（确认 / 观察）</h2>" + _plan_cards(result)
        + f"<h2>候选排名（前 {min(top_n, len(result.picks))} 只）</h2>"
        + _candidate_table(result, top_n)
        + "<h2>未通过原因统计</h2>" + _reject_table(result)
        + allowed + notes + error_line
        + "<footer>本报告为技术形态筛选结果，不构成投资建议，不保证收益；"
          "价格为不复权收盘价，除权除息日前后请人工核对；"
          "止损、目标与股数按收盘价与止损价的差额计算（1% 单笔风险 / 10% 单票上限），"
          "实际在买入区间上沿成交、加上滑点与手续费后风险会略大。"
          "止损取「回调低点下方 2%」与「MA20×0.995」中更低的一条，"
          "因此部分标的的止损距离会超过 10%，股数会相应减少。"
          "历史形态不代表未来表现。</footer>")

    return ("<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<title>A股强势回调低吸 · 扫描报告</title>"
            f"<style>{_CSS}</style></head><body>{body}</body></html>")
