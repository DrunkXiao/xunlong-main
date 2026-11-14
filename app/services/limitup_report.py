"""涨停四阶段报告：单文件深色 HTML（内嵌样式，无 JS、无外链，可直接发出去或打印）。

章节目录固定，和技能里的规范一致（不要随手改，改了就两处对不上）：

    A 市场环境 / B 四阶段股票池（含待确认）/ C 最终精选 / D 次日交易计划
    E 重点关注名单 / F 风险提示 / G 股票池状态与复盘

配色是 **A 股习惯**：涨 = 红 `#ff4d4f`，跌 = 绿 `#12c48b`，平/不可得 = 灰。
不要改成欧美习惯，改了看盘的人会读反。
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from app.services.limitup import DIM_LABEL, LAUNCH_CHECKS, STAGE_LABEL
from app.services.limitup_scanner import ScanResult

UP = "#ff4d4f"
DOWN = "#12c48b"
DIM = "#8a97ad"

_CSS = f"""
:root {{
  --bg:#0a0e15; --card:#131a26; --card2:#182130; --line:#232d3d; --line2:#2e3a4e;
  --txt:#e7edf7; --txt2:#9dabc2; --txt3:#68768c;
  --up:{UP}; --down:{DOWN}; --amber:#f0b429; --blue:#4c8dff;
  --mono:"SF Mono","JetBrains Mono",Consolas,"Courier New",monospace;
}}
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:var(--bg); color:var(--txt); padding:30px 22px 56px; line-height:1.6;
        font:13px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif; }}
.wrap {{ max-width:1180px; margin:0 auto; }}
h1 {{ font-size:26px; font-weight:700; display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; }}
h1 .date {{ font-family:var(--mono); font-size:15px; color:var(--blue); }}
.flow {{ color:var(--txt2); font-size:12.5px; margin-top:7px; }}
.flow b {{ color:var(--txt); }}
.flow i {{ font-style:normal; color:var(--line2); margin:0 7px; }}
.meta {{ display:flex; flex-wrap:wrap; gap:6px 20px; margin-top:12px;
         font-family:var(--mono); font-size:11.5px; color:var(--txt3); }}
.meta b {{ color:var(--txt2); font-weight:500; }}
h2 {{ font-size:14.5px; font-weight:650; margin:32px 0 10px;
      display:flex; align-items:center; gap:9px; }}
h2::before {{ content:""; width:3px; height:13px; background:var(--blue); border-radius:2px; }}
h2 .en {{ font-family:var(--mono); font-size:10px; letter-spacing:.14em; color:var(--txt3); }}
.lede {{ font-size:12.2px; color:var(--txt2); margin-bottom:12px; }}
.kpis {{ display:grid; grid-template-columns:repeat(6,1fr); gap:9px; }}
.kpi {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:11px 12px; }}
.kpi .lab {{ font-size:10.8px; color:var(--txt3); }}
.kpi .val {{ font-family:var(--mono); font-size:22px; font-weight:700; line-height:1.2; }}
.kpi .val small {{ font-size:11px; color:var(--txt3); font-weight:500; }}
.quotes {{ display:grid; grid-template-columns:repeat(3,1fr); gap:9px; margin-top:9px; }}
.quote {{ background:var(--card); border:1px solid var(--line); border-radius:8px;
          padding:9px 12px; display:flex; justify-content:space-between; align-items:baseline; }}
.quote .nm {{ font-size:12px; color:var(--txt2); }}
.quote .px {{ font-family:var(--mono); font-size:15px; font-weight:600; }}
.mktline {{ margin-top:9px; background:var(--card); border:1px solid var(--line); border-radius:8px;
            padding:10px 13px; display:flex; flex-wrap:wrap; gap:6px 22px;
            font-size:12px; color:var(--txt2); }}
.mktline b {{ font-family:var(--mono); color:var(--txt); }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
         border:1px solid var(--line); font-size:12px; }}
th, td {{ padding:5px 8px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ background:var(--card2); color:var(--txt2); font-weight:600; font-size:11.5px; }}
td.num {{ text-align:right; font-family:var(--mono); }}
tr:hover td {{ background:rgba(76,141,255,.05); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .dim {{ color:var(--txt3); }}
.chip {{ display:inline-block; padding:0 6px; border-radius:3px; font-size:11px;
         border:1px solid var(--line2); color:var(--txt2); }}
.chip.s1 {{ border-color:rgba(76,141,255,.5); color:#8fb4ff; }}
.chip.s2 {{ border-color:rgba(240,180,41,.5); color:#f0c866; }}
.chip.s3 {{ border-color:rgba(166,139,255,.5); color:#c0acff; }}
.chip.s4 {{ border-color:rgba(255,77,79,.5); color:#ff8a8c; }}
.cards {{ display:flex; flex-wrap:wrap; gap:11px; }}
.card {{ flex:1 1 340px; background:var(--card); border:1px solid var(--line);
         border-radius:8px; padding:12px 14px; }}
.card h3 {{ font-size:13.5px; margin-bottom:7px; display:flex; align-items:center; gap:7px; }}
.kv {{ display:grid; grid-template-columns:76px 1fr; gap:2px 8px; font-size:11.8px; }}
.kv b {{ color:var(--txt3); font-weight:600; }}
.kv span {{ font-family:var(--mono); }}
.empty {{ background:var(--card); border:1px dashed var(--line2); border-radius:8px;
          padding:12px 14px; font-size:12.2px; color:var(--txt3); }}
.warn {{ margin-top:14px; padding:11px 14px; border-radius:8px; font-size:12.4px;
         background:rgba(240,180,41,.10); border:1px solid rgba(240,180,41,.32);
         border-left:3px solid var(--amber); color:#f4dfae; }}
.warn b {{ color:var(--amber); }}
footer {{ margin-top:26px; color:var(--txt3); font-size:11px; line-height:1.7; }}
"""


def _num(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def _pct(value, cls: str = "") -> str:
    if value is None:
        return "<span class='dim'>—</span>"
    tone = "up" if value > 0 else "down" if value < 0 else "dim"
    return f"<span class='{tone} {cls}'>{value:+.2f}%</span>"


def _stage_chip(stage: str) -> str:
    return (f"<span class='chip {stage.lower()}'>{escape(STAGE_LABEL.get(stage, stage))}</span>")


def _reasons(item: dict) -> str:
    parts = []
    snap = item["snap"]
    if item.get("carry"):
        parts.append(f"承接 {item['carry'].get('carry_type')}")
    if item.get("launch"):
        parts.append(f"整理 {item['launch'].get('cons_days')} 日")
    parts.append(f"量能 {_num(snap.get('vol_ratio5'))} 倍")
    parts.append(f"换手 {_num(snap.get('turnover'))}%")
    parts.append(f"质量 {snap.get('quality') or '—'}")
    return " · ".join(parts)


def _market_section(result: ScanResult) -> str:
    market = result.market or {}
    indices = result.indices or {}
    amounts = result.amounts or {}
    kpis = [
        ("涨停家数", market.get("limit_up_count"), "", UP),
        ("其中首板", market.get("first_board_count"), "", UP),
        ("触板未封", market.get("touched_count"), "", DIM),
        ("炸板率", market.get("fail_rate"), "%", DOWN),
        ("情绪分", market.get("sentiment_score"), "", "#f0b429"),
        ("最高连板", market.get("max_consecutive"), "板", UP),
    ]
    kpi_html = "".join(
        f"<div class='kpi'><div class='lab'>{escape(label)}</div>"
        f"<div class='val' style='color:{color}'>{_num(value, 1 if isinstance(value, float) else 0)}"
        f"<small>{unit}</small></div></div>"
        for label, value, unit, color in kpis)
    quotes = "".join(
        f"<div class='quote'><span class='nm'>{escape(name)}</span>"
        f"<span class='px'>{_num(info.get('close'))}</span>{_pct(info.get('pct'))}</div>"
        for name, info in indices.items())
    leaders = " · ".join(
        f"{escape(x['name'])} {_pct(x.get('change'))}（涨停 {x.get('count') or 0}）"
        for x in (market.get("sector_leaders") or [])[:6])
    lines = market.get("main_lines") or []
    return f"""
<h2>A 市场环境 <span class="en">MARKET</span></h2>
<p class="lede">情绪分与炸板率是<b>日线代理口径</b>：炸板率 = 盘中触及涨停但收盘未封住 / 当日触及涨停家数，
不是交易所逐笔统计。板块强度用 {escape(str(market.get('sector_caliber') or '行业指数'))}。</p>
<div class="kpis">{kpi_html}</div>
<div class="quotes">{quotes}</div>
<div class="mktline">
  <span>沪深成交额 <b>{_num(amounts.get('total_yi'), 0)}</b> 亿元</span>
  <span>上涨 <b class="up">{market.get('up_count', 0)}</b> / 下跌 <b class="down">{market.get('down_count', 0)}</b></span>
  <span>情绪 <b>{escape(str(market.get('mood') or '—'))}</b> · 风险 <b>{escape(str(market.get('risk_level') or '—'))}</b></span>
  <span>主线 <b>{escape('、'.join(lines) if lines else '—')}</b></span>
</div>
<div class="mktline"><span>板块涨幅前列：{leaders or '<span class="dim">—</span>'}</span></div>
"""


def _stage_table(items: list[dict], limit: int) -> str:
    if not items:
        return "<div class='empty'>本阶段今日无标的（不降标准凑数）</div>"
    rows = []
    for index, item in enumerate(items[:limit], 1):
        snap = item["snap"]
        score = item["score"]
        rows.append(
            "<tr>"
            f"<td class='num'>{index}</td>"
            f"<td>{escape(snap['code'])}</td>"
            f"<td>{escape(snap['name'])}</td>"
            f"<td>{escape(snap.get('sector') or '—')}</td>"
            f"<td class='num'>{_num(snap.get('close'))}</td>"
            f"<td class='num'>{_pct(snap.get('pct'))}</td>"
            f"<td class='num'>{_num(snap.get('vol_ratio5'))}</td>"
            f"<td class='num'>{_num(snap.get('turnover'))}</td>"
            f"<td class='num'>{_num(score.get('normalized'), 1)}</td>"
            f"<td>{escape(str(score.get('grade') or '—'))}</td>"
            f"<td class='dim'>{escape(_reasons(item))}</td>"
            "</tr>")
    return (
        "<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>行业</th><th>收盘</th>"
        "<th>涨幅</th><th>5日量能</th><th>换手</th><th>评分</th><th>评级</th><th>依据</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _pool_section(result: ScanResult, limit: int) -> str:
    blocks = []
    for stage in ("S1", "S2", "S3", "S4"):
        items = result.groups.get(stage) or []
        blocks.append(
            f"<h3 style='font-size:12.5px;margin:16px 0 6px;color:var(--txt2)'>"
            f"{_stage_chip(stage)} <span class='dim'>共 {len(items)} 只</span></h3>"
            + _stage_table(items, limit))
    # 待确认：缩量整理中，只给触发条件，不给启动结论
    pending = result.pending or []
    if pending:
        rows = []
        for record in pending[:limit]:
            launch = record.get("launch") or {}
            rows.append(
                "<tr>"
                f"<td>{escape(record.get('code') or '')}</td>"
                f"<td>{escape(record.get('name') or '')}</td>"
                f"<td class='num'>{_num(record.get('last_close'))}</td>"
                f"<td class='num'>{launch.get('cons_days')}</td>"
                f"<td class='num'>{_num(launch.get('cons_high'))}</td>"
                f"<td class='num'>{_num(launch.get('vol_contract'))}</td>"
                f"<td class='dim'>{escape(str(launch.get('detail') or ''))}</td>"
                "</tr>")
        blocks.append(
            "<h3 style='font-size:12.5px;margin:16px 0 6px;color:var(--txt2)'>"
            "待确认（缩量整理中） <span class='dim'>只给触发条件，不判启动</span></h3>"
            "<table><thead><tr><th>代码</th><th>名称</th><th>最新价</th><th>整理日</th>"
            "<th>整理上沿</th><th>缩量比</th><th>状态</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")

    reject = result.reject or {}
    if reject.get("total"):
        counts = " · ".join(f"{escape(k)} {v}" for k, v in (reject.get("counts") or {}).items())
        blocks.append(
            f"<div class='mktline' style='margin-top:12px'>三倍量未通过 "
            f"<b>{reject.get('total')}</b> 只（可按原因复核）：{counts or '—'}</div>")
    return "\n".join(blocks)


def _pick_cards(result: ScanResult) -> str:
    picks = result.picks or []
    if not picks:
        return ("<div class='empty'>今日没有符合条件的标的。"
                "按规则不降标准凑数——建议继续观察，等首板/三倍量重新出现。</div>")
    cards = []
    for index, item in enumerate(picks, 1):
        snap, plan, score = item["snap"], item["plan"], item["score"]
        verify = " + ".join(DIM_LABEL[k] for k in score.get("verified_dims") or [])
        unverified = "、".join(DIM_LABEL.get(k, k) for k in score.get("unverified") or []) or "无"
        checks = item.get("launch", {}).get("checks") if item.get("launch") else None
        check_html = ""
        if checks:
            check_html = "<div class='kv' style='margin-top:6px'>" + "".join(
                f"<b>{escape(LAUNCH_CHECKS[k])}</b><span class='{'up' if v else 'down'}'>"
                f"{'✓' if v else '✗'}</span>"
                for k, v in checks.items()) + "</div>"
        cards.append(f"""
<div class="card">
  <h3>{index}. {escape(snap['name'])} <span class="dim">{escape(snap['code'])}</span>
      {_stage_chip(item['stage'])}
      <span style="margin-left:auto;font-family:var(--mono)">{_num(score.get('normalized'), 1)} 分
      <span class="dim">{escape(str(score.get('grade') or ''))}</span></span></h3>
  <div class="kv">
    <b>收盘</b><span>{_num(snap.get('close'))} {_pct(snap.get('pct'))}</span>
    <b>支撑 / 止损</b><span>{_num(plan.get('support'))} / {_num(plan.get('stop'))}</span>
    <b>目标 1 / 2</b><span class="up">{_num(plan.get('target1'))} / {_num(plan.get('target2'))}</span>
    <b>压力</b><span>{_num(plan.get('resistance'))}</span>
    <b>风险</b><span>{escape(str(item.get('risk') or '—'))} · 每股风险 {_num(plan.get('risk_per_share'))}
        （{_num(plan.get('risk_pct'), 2)}%）</span>
    <b>已验证</b><span class="dim">{escape(verify)}</span>
    <b>未验证</b><span class="dim">{escape(unverified)}</span>
    <b>完整度</b><span>{_num(score.get('completeness'), 2)}（置信 {escape(str(score.get('confidence') or '—'))}）</span>
  </div>
  {check_html}
  <div class="dim" style="margin-top:7px;font-size:11.5px">{escape(item.get('reason') or '')}</div>
</div>""")
    return "<div class='cards'>" + "".join(cards) + "</div>"


def _plan_table(result: ScanResult) -> str:
    picks = result.picks or []
    if not picks:
        return "<div class='empty'>无交易计划（今日无入选标的）。</div>"
    rows = []
    for item in picks:
        snap, plan = item["snap"], item["plan"]
        rows.append(
            "<tr>"
            f"<td>{escape(snap['code'])} {escape(snap['name'])}</td>"
            f"<td class='num'>{_num(snap.get('close'))}</td>"
            f"<td class='num'>{_num(plan.get('support'))}</td>"
            f"<td class='num down'>{_num(plan.get('stop'))}</td>"
            f"<td class='num up'>{_num(plan.get('target1'))}</td>"
            f"<td class='num up'>{_num(plan.get('target2'))}</td>"
            f"<td class='dim' style='white-space:normal'>{escape(item.get('next_trigger') or '')}</td>"
            f"<td class='dim' style='white-space:normal'>{escape(plan.get('invalidation') or '')}</td>"
            "</tr>")
    return (
        "<table><thead><tr><th>标的</th><th>现价</th><th>支撑</th><th>止损</th>"
        "<th>目标1（1.5R）</th><th>目标2（2.5R）</th><th>触发条件</th><th>失效条件</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _watch_section(result: ScanResult) -> str:
    """重点关注：把"看着热但其实危险"的票单独列出来（不推荐，只提示风险）。"""
    risky = []
    for item in result.all_candidates:
        snap = item["snap"]
        reasons = []
        if (snap.get("pos60") or 0) >= 0.9:
            reasons.append(f"60日区间位置 {_num(snap.get('pos60'), 2)}（高位）")
        if (snap.get("vol_ratio5") or 0) >= 6:
            reasons.append(f"5日量能 {_num(snap.get('vol_ratio5'))} 倍（巨量）")
        if snap.get("quality") == "C":
            reasons.append("涨停质量 C 级（日线代理）")
        if (snap.get("pct") or 0) >= 9.5 and item["stage"] == "S1":
            reasons.append("首板未获量能确认")
        if reasons:
            risky.append((item, reasons))
    if not risky:
        return "<div class='empty'>无高位巨量或质量存疑标的。</div>"
    rows = "".join(
        f"<tr><td>{escape(i['snap']['code'])} {escape(i['snap']['name'])}</td>"
        f"<td>{_stage_chip(i['stage'])}</td>"
        f"<td class='dim' style='white-space:normal'>{escape('；'.join(r))}</td></tr>"
        for i, r in risky[:20])
    return ("<table><thead><tr><th>标的</th><th>阶段</th><th>风险点</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def _risk_section(result: ScanResult) -> str:
    items = [
        "不追逐高开超过 +5% 的标的：开盘溢价已经把风险报酬比吃掉了。",
        "盘中触板 ≠ 收盘涨停；本报告的涨停一律按**收盘价**判定。",
        "3 倍量只是量价事实，不含任何资金身份信息；本报告不就资金归属作推断。",
        "首封时间、炸板次数、封单金额、分时均价本地日线拿不到，报告里一律「—」，不填 0、不推测。",
        "承接失败（S7）/ 突破失败（S8）后不再给高评级，只保留历史记录。",
        "A 股 T+1 且涨停不保证成交：计划里的买入价是条件，不是成交承诺。",
        "板块 5 日跌幅超过 3% 视为退潮，不强行交易。",
    ]
    notes = "".join(f"<li>{escape(n)}</li>" for n in items)
    gaps = "、".join(result.market.get("missing") or []) or "首封时间/炸板次数/封单金额/分时均价"
    return f"""
<h2>F 风险提示 <span class="en">RISK</span></h2>
<p class="lede">本报告的评分是<b>按阶段门控的归一化分</b>：S1/S2 上限按 55 分权重归一，
S3 按 75，S4 按 100。所以早期阶段的 90 分不等于"全阶段 S 级"——必须同时看已验/未验证维度与数据完整度。</p>
<ul style="font-size:12.3px;color:var(--txt2);padding-left:20px;line-height:1.85">{notes}</ul>
<div class="warn"><b>数据缺口（本地日线口径）</b>：{escape(gaps)} 这五类字段需要分时与逐笔，
本地 vipdoc 数据不提供，因此完整度上限约 71%，置信等级最高为 C。
报告里所有涉及封板质量的结论都标了「日线代理」。</div>
"""


def _pool_status_section(result: ScanResult) -> str:
    summary = result.pool_summary or {}
    text = " · ".join(f"{escape(k)} {v}" for k, v in summary.items()) or "股票池为空"
    review = result.review or {}
    rows = []
    for label, agg in (review.get("by_state") or {}).items():
        cells = "".join(
            f"<td class='num'>{_num(agg.get(h, {}).get('win'), 1)}"
            f"<span class='dim'>/{agg.get(h, {}).get('n', 0)}</span></td>"
            for h in ("t1", "t3", "t5"))
        rows.append(f"<tr><td>{escape(label)}</td>{cells}</tr>")
    review_html = (
        "<table><thead><tr><th>状态</th><th>T+1 胜率/样本</th><th>T+3</th><th>T+5</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
        if rows else "<div class='empty'>池内还没有可统计的记录（首板日之后至少需要 1 个交易日）。</div>")
    caveat = review.get("caveat") or ""
    return f"""
<h2>G 股票池状态与复盘 <span class="en">POOL</span></h2>
<div class="mktline"><span>状态统计：{text}</span></div>
<p class="lede" style="margin-top:10px">复盘胜率按<b>首板日收盘价</b>与之后第 N 个交易日收盘价对比，只统计已有数据的样本。</p>
{review_html}
<div class="warn">{escape(caveat)}{escape('（复盘统计失败：' + str(review.get('error')) + '）') if review.get('error') else ''}</div>
"""


def render(result: ScanResult, config: dict | None = None) -> str:
    """整份报告。只陈述事实：数据源、数据日、参数、每只标的的证据与计划。"""
    counts = result.counts
    notes = "".join(f"<li>{escape(n)}</li>" for n in (result.notes or []))
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = " · ".join(f"{escape(k)} {v}" for k, v in (result.params or {}).items())
    stale = result.stale_days
    stale_tag = (f"<span style='color:{DOWN if (stale or 0) <= 3 else UP}'>滞后 {stale} 个自然日</span>"
                 if stale is not None else "—")

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>涨停四阶段 · {result.data_date} · 寻龙盯盘</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>涨停四阶段选股 <span class="date">{result.data_date or '—'}</span></h1>
<div class="flow">
  <b>首板发现</b><i>→</i><b>三倍量确认</b><i>→</i><b>次日承接</b><i>→</i><b>二次启动</b>
  <span class="dim">｜四阶段：首板 {counts.get('S1', 0)} · 三倍量 {counts.get('S2', 0)} ·
  承接 {counts.get('S3', 0)} · 启动 {counts.get('S4', 0)} · 精选 {len(result.picks or [])}</span>
</div>
<div class="meta">
  <span>数据源 <b>{escape(result.source_label or '本地通达信日线')}</b></span>
  <span>数据日 <b>{result.data_date or '—'}</b> {stale_tag}</span>
  <span>股票池 <b>{result.universe}</b> · 有效快照 <b>{result.scanned}</b> · 跳过 <b>{result.errors}</b></span>
  <span>耗时 <b>{result.elapsed:.1f}s</b></span>
  <span>生成 <b>{generated}</b></span>
  <span>等级 <b>C（日线收盘口径）</b></span>
</div>
<div class="warn"><b>口径</b>：{escape('；'.join(result.notes or [])) or '本地日线收盘口径'}<br>
参数：{params}</div>
{_market_section(result)}
<h2>B 四阶段股票池 <span class="en">STAGES</span></h2>
<p class="lede">每个阶段只对<b>已经发生</b>的事实计分：没涨停就不进首板，没放量就不进三倍量，
没突破就只写「等待启动」。同一只票只出现在它当前最高的阶段里。</p>
{_pool_section(result, 40)}
<h2>C 最终精选 <span class="en">PICKS</span></h2>
<p class="lede">容量上限：二次启动 5 只、承接 2 只、三倍量 3 只、首板 3 只，合计不超过 10 只；
不足不填满，一只没有就说没有。</p>
{_pick_cards(result)}
<h2>D 次日交易计划 <span class="en">PLAN</span></h2>
<p class="lede">止损 = 支撑 − 0.5×ATR14（若高于现价则改为现价 − 1.2×ATR14）；
目标按 1.5R / 2.5R 折算。所有价格来自真实日线，不是估算值。</p>
{_plan_table(result)}
<h2>E 重点关注名单 <span class="en">WATCH</span></h2>
<p class="lede">列出来的是<b>风险点</b>，不是推荐：高位巨量、涨停质量存疑的票要么不做，要么只在计划价内做。</p>
{_watch_section(result)}
{_risk_section(result)}
{_pool_status_section(result)}
<footer>
  本页由「寻龙盯盘」生成，仅用于研究与复盘，不构成投资建议，不自动下单。<br>
  数据来源：本地通达信 vipdoc 日线（{escape(str(result.source_label or ''))}）。
  涨=红 #{UP[1:]} / 跌=绿 #{DOWN[1:]}（A 股习惯）。
</footer>
</div></body></html>
"""
