"""量学板枪报告：单文件深色 HTML（内嵌样式，无 JS、无外链，可直接发出去或打印）。

章节目录固定，和 limitup_report 的规范一致（改了就两处对不上）：

    A 扫描口径 / B 五阶段漏斗 / C 候选分组 / D 精选与买点 / E 逐只结构与计划 / F 风险提示

配色是 **A 股习惯**：涨 = 红 `#ff4d4f`，跌 = 绿 `#12c48b`，平/不可得 = 灰。
不要改成欧美习惯，改了看盘的人会读反。
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from app.services.banqiang import DIM_LABEL, STATE_GROUPS
from app.services.banqiang_scanner import ScanResult

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
body {{ background:var(--bg); color:var(--txt); padding:30px 22px 56px;
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
h2 {{ font-size:14.5px; font-weight:650; margin:32px 0 10px; display:flex; align-items:center; gap:9px; }}
h2::before {{ content:""; width:3px; height:13px; background:var(--blue); border-radius:2px; }}
h2 .en {{ font-family:var(--mono); font-size:10px; letter-spacing:.14em; color:var(--txt3); }}
.lede {{ font-size:12.2px; color:var(--txt2); margin-bottom:12px; }}
.warn {{ margin-top:12px; background:rgba(240,180,41,.08); border:1px solid rgba(240,180,41,.3);
         border-radius:8px; padding:10px 13px; font-size:11.8px; color:#e8cf9a; }}
.kpis {{ display:grid; grid-template-columns:repeat(6,1fr); gap:9px; }}
.kpi {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:11px 12px; }}
.kpi .lab {{ font-size:10.8px; color:var(--txt3); }}
.kpi .val {{ font-family:var(--mono); font-size:22px; font-weight:700; line-height:1.2; }}
.kpi .val small {{ font-size:11px; color:var(--txt3); font-weight:500; }}
table {{ width:100%; border-collapse:collapse; background:var(--card);
         border:1px solid var(--line); font-size:12px; }}
th, td {{ padding:5px 8px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
th {{ background:var(--card2); color:var(--txt2); font-weight:600; font-size:11.5px; }}
td.num {{ text-align:right; font-family:var(--mono); }}
tr:hover td {{ background:rgba(76,141,255,.05); }}
.up {{ color:var(--up); }} .down {{ color:var(--down); }} .dim {{ color:var(--txt3); }}
.chip {{ display:inline-block; padding:0 6px; border-radius:3px; font-size:11px;
         border:1px solid var(--line2); color:var(--txt2); }}
.chip.buy {{ border-color:rgba(255,77,79,.5); color:#ff8a8c; }}
.chip.near {{ border-color:rgba(240,180,41,.5); color:#f0c866; }}
.chip.pull {{ border-color:rgba(76,141,255,.5); color:#8fb4ff; }}
.chip.form {{ border-color:var(--line2); color:var(--txt2); }}
.cards {{ display:flex; flex-wrap:wrap; gap:11px; }}
.card {{ flex:1 1 340px; background:var(--card); border:1px solid var(--line);
         border-radius:8px; padding:12px 14px; }}
.card h3 {{ font-size:13.5px; margin-bottom:7px; display:flex; align-items:center; gap:7px; }}
.kv {{ display:grid; grid-template-columns:78px 1fr; gap:2px 8px; font-size:11.8px; }}
.kv b {{ color:var(--txt3); font-weight:600; }}
.kv span {{ font-family:var(--mono); }}
.empty {{ background:var(--card); border:1px dashed var(--line2); border-radius:8px;
          padding:14px; color:var(--txt3); font-size:12px; }}
footer {{ margin-top:40px; padding-top:14px; border-top:1px solid var(--line);
          color:var(--txt3); font-size:11.5px; line-height:1.8; }}
"""


def _num(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "—"


def _chip(state: str) -> str:
    cls = {"买入观察": "buy", "买点临近": "near", "缩量回调中": "pull", "形态形成中": "form"}.get(state, "")
    return f"<span class='chip {cls}'>{escape(state or '—')}</span>"


def _plan_of(item: dict) -> dict:
    return item.get("plan") or {}


def _row(index: int, item: dict) -> str:
    plan = _plan_of(item)
    score = item.get("score") or {}
    pct = item.get("pct")
    pct_cls = "up" if (pct or 0) > 0 else "down" if (pct or 0) < 0 else "dim"
    return f"""<tr>
<td class="num dim">{index}</td>
<td class="dim">{escape(str(item.get('code') or ''))}</td>
<td>{escape(str(item.get('name') or ''))}</td>
<td>{_chip(item.get('state'))}</td>
<td class="num">{_num(score.get('total'), 0)}</td>
<td>{escape(score.get('grade') or '—')}</td>
<td class="num">{_num(item.get('close'))}</td>
<td class="num {pct_cls}">{'—' if pct is None else f'{pct:+.2f}%'}</td>
<td class="num">{_num((item.get('s3') or {}).get('ratio'))}</td>
<td>{escape((item.get('s5') or {}).get('buy_type') or '—')}</td>
<td class="num">{_num((item.get('s5') or {}).get('buy_trigger_price'))}</td>
<td class="num down">{_num(plan.get('stop'))}</td>
<td>{escape(item.get('industry') or '—')}</td>
</tr>"""


def _group_table(result: ScanResult, items: list, limit: int = 60) -> str:
    if not items:
        return "<div class='empty'>该分组今日没有标的。</div>"
    rows = "".join(_row(i + 1, it) for i, it in enumerate(items[:limit]))
    more = (f"<div class='lede dim'>仅显示前 {limit} 只，共 {len(items)} 只。</div>"
            if len(items) > limit else "")
    return f"""<table>
<tr><th>#</th><th>代码</th><th>名称</th><th>状态</th><th>评分</th><th>评级</th><th>收盘</th>
<th>涨幅</th><th>回撤比</th><th>买点</th><th>触发价</th><th>止损</th><th>行业</th></tr>
{rows}</table>{more}"""


def _kpis(result: ScanResult) -> str:
    counts = result.counts
    cells = [
        ("股票池", result.universe, "只"),
        ("有效快照", result.scanned, "只"),
        ("买入观察", counts.get("买入观察", 0), "只"),
        ("买点临近", counts.get("买点临近", 0), "只"),
        ("缩量回调中", counts.get("缩量回调中", 0), "只"),
        ("形态形成中", counts.get("形态形成中", 0), "只"),
    ]
    return "<div class='kpis'>" + "".join(
        f"<div class='kpi'><div class='lab'>{label}</div>"
        f"<div class='val'>{value}<small> {unit}</small></div></div>"
        for label, value, unit in cells) + "</div>"


def _funnel(result: ScanResult) -> str:
    funnel = result.funnel or {}
    if not funnel:
        return "<div class='empty'>没有筛除记录。</div>"
    total = sum(funnel.values()) or 1
    rows = "".join(
        f"<tr><td>{escape(reason)}</td><td class='num'>{count}</td>"
        f"<td class='num dim'>{count / total * 100:.1f}%</td></tr>"
        for reason, count in funnel.items())
    return f"""<table><tr><th>筛除原因（推进到哪一阶段倒下）</th><th>只数</th><th>占比</th></tr>
{rows}</table>
<p class="lede" style="margin-top:10px">「板日-最近 5 日无涨停」占大头是正常的：板日本就稀少，
这是漏斗第一层，不是数据坏了。</p>"""


def _picks(result: ScanResult) -> str:
    picks = result.picks or []
    if not picks:
        return ("<div class='empty'>今天没有买点成立或临近的标的。结构在的票会留在「缩量回调中」，"
                "等触发条件满足再动手，不追高。</div>")
    cards = []
    for item in picks[:12]:
        plan = _plan_of(item)
        score = item.get("score") or {}
        s5 = item.get("s5") or {}
        dims = score.get("dims") or {}
        dim_text = " · ".join(f"{DIM_LABEL.get(k, k)} {v.get('score')}/{v.get('max')}"
                              for k, v in dims.items())
        cards.append(f"""<div class="card">
<h3>{escape(item.get('name') or '')} <span class="dim">{escape(item.get('symbol') or '')}</span>
{_chip(item.get('state'))} <span class="dim">{score.get('total')} 分 {escape(score.get('grade') or '')}</span></h3>
<div class="kv">
<b>板 / 枪</b><span>{item.get('board_date')} / {item.get('gun_date')}</span>
<b>买点</b><span>{escape(s5.get('buy_type') or '—')} 触发 {_num(s5.get('buy_trigger_price'))}</span>
<b>条件</b><span>{escape(s5.get('buy_cond') or '—')}</span>
<b>止损</b><span class="down">{_num(plan.get('stop'))}（回调低点下 1%）</span>
<b>结构底线</b><span>{_num(plan.get('deep_floor'))}（板日开盘）</span>
<b>目标 1/2/3</b><span class="up">{_num(plan.get('target1'))} / {_num(plan.get('target2'))} /
{_num(plan.get('target3'))}</span>
<b>回撤比</b><span>{_num((item.get('s3') or {}).get('ratio'))}</span>
<b>盈亏比</b><span>1 : {_num(plan.get('rr'))}
{'' if plan.get('target1_valid', True) else '<span class="dim">（目标 1 已在现价下方，看目标 2/3）</span>'}</span>
</div>
<div class="lede dim" style="margin:8px 0 0">{escape(dim_text)}</div>
</div>""")
    return "<div class='cards'>" + "".join(cards) + "</div>"


def _plans(result: ScanResult) -> str:
    picks = result.picks or []
    if not picks:
        return "<div class='empty'>没有可列计划的标的。</div>"
    rows = "".join(
        f"<tr><td>{escape(it.get('name') or '')}</td><td class='dim'>{escape(it.get('code') or '')}</td>"
        f"<td>{escape((it.get('s5') or {}).get('buy_type') or '—')}</td>"
        f"<td class='num'>{_num((it.get('s5') or {}).get('buy_trigger_price'))}</td>"
        f"<td class='num down'>{_num(_plan_of(it).get('stop'))}</td>"
        f"<td class='num up'>{_num(_plan_of(it).get('target1'))}</td>"
        f"<td class='num up'>{_num(_plan_of(it).get('target2'))}</td>"
        f"<td class='num up'>{_num(_plan_of(it).get('target3'))}</td>"
        f"<td class='num'>{_num(_plan_of(it).get('rr'))}</td>"
        f"<td>{escape(_plan_of(it).get('invalidate') or '—')}</td></tr>"
        for it in picks[:30])
    return f"""<table>
<tr><th>名称</th><th>代码</th><th>买点</th><th>触发价</th><th>止损</th><th>目标1</th><th>目标2</th>
<th>目标3</th><th>盈亏比</th><th>失效条件</th></tr>
{rows}</table>
<p class="lede" style="margin-top:10px">止损 = 回调低点 ×(1−1%)；目标 1 = 板枪顶部，
目标 2 = 左侧大阴实顶，目标 3 = 250 日高量柱最高价（已在下方时退化）。不含滑点与手续费。</p>"""


def _structure(result: ScanResult) -> str:
    items = result.all_candidates or []
    items = [it for it in items if not it.get("early")][:20]
    if not items:
        return "<div class='empty'>没有走完五阶段的结构可列。</div>"
    cards = []
    for it in items:
        s1, s2, s3, s4 = it.get("s1") or {}, it.get("s2") or {}, it.get("s3") or {}, it.get("s4") or {}
        cards.append(f"""<div class="card">
<h3>{escape(it.get('name') or '')} <span class="dim">{escape(it.get('symbol') or '')}</span>
{_chip(it.get('state'))}</h3>
<div class="kv">
<b>板日</b><span>{it.get('board_date')} {_num(it.get('s1_close'))}（{s1.get('board_streak')} 连板，
量 {_num(s1.get('board_vol_mult'))} 倍）</span>
<b>枪日</b><span>{it.get('gun_date')} {escape(s2.get('gun_type') or '')} {escape(s2.get('gun_grade') or '')} 级，
上影 {_num((s2.get('gun_shadow_ratio') or 0) * 100, 0)}%，量/板 {_num(s2.get('gun_vol_mult'))} 倍</span>
<b>回调</b><span>{s3.get('pull_days')} 日，首日缩量 {_num((s3.get('shrink_first') or 0) * 100, 0)}%，
低点 {_num(s3.get('pull_low'))}，止跌：{escape(s3.get('stop_signal') or '无')}</span>
<b>阶梯</b><span>{s4.get('step_level')} 级 · {escape(s4.get('step_tier') or '')}</span>
<b>凹间峰</b><span>{escape(s4.get('gap_state') or '—')}（{_num(s4.get('gap_peak'))}）</span>
<b>上方套牢</b><span>{_num((s4.get('trap_ratio') or 0) * 100, 0)}% ·
下方获利 {_num((s4.get('profit_ratio') or 0) * 100, 0)}%</span>
</div>
</div>""")
    return "<div class='cards'>" + "".join(cards) + "</div>"


def render(result: ScanResult, config: dict | None = None) -> str:
    """整份报告。只陈述事实：数据源、数据日、参数、每只标的的证据与计划。"""
    counts = result.counts
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = " · ".join(f"{escape(k)} {v}" for k, v in (result.params or {}).items())
    stale = result.stale_days
    stale_tag = (f"<span style='color:{DOWN if (stale or 0) <= 3 else UP}'>滞后 {stale} 个自然日</span>"
                 if stale is not None else "—")
    notes = "；".join(result.notes or [])

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>量学板枪 · {result.data_date} · 寻龙盯盘</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>量学板枪选股 <span class="date">{result.data_date or '—'}</span></h1>
<div class="flow">
  <b>涨停首板</b><i>→</i><b>放量出枪</b><i>→</i><b>缩量回调</b><i>→</i><b>黄金阶梯/筹码</b>
  <i>→</i><b>二次启动买点</b>
  <span class="dim">｜买点已触发 {counts.get('买入观察', 0)} · 临近 {counts.get('买点临近', 0)} ·
  回调中 {counts.get('缩量回调中', 0)} · 形成中 {counts.get('形态形成中', 0)}</span>
</div>
<div class="meta">
  <span>数据源 <b>{escape(result.source_label or '本地通达信日线')}</b></span>
  <span>数据日 <b>{result.data_date or '—'}</b> {stale_tag}</span>
  <span>股票池 <b>{result.universe}</b> · 有效快照 <b>{result.scanned}</b> · 跳过 <b>{result.errors}</b></span>
  <span>耗时 <b>{result.elapsed:.1f}s</b></span>
  <span>生成 <b>{generated}</b></span>
  <span>等级 <b>C（日线收盘口径）</b></span>
</div>
<div class="warn"><b>口径</b>：{escape(notes) or '本地日线收盘口径'}<br>参数：{params}</div>

<h2>A 扫描口径 <span class="en">SCOPE</span></h2>
{_kpis(result)}

<h2>B 五阶段漏斗 <span class="en">FUNNEL</span></h2>
<p class="lede">每一层都是硬性条件，不为了多出票而放宽。要更早的信号只会更接近追高。</p>
{_funnel(result)}

<h2>C 候选分组 <span class="en">GROUPS</span></h2>
{''.join(f"<h3 style='margin:16px 0 8px;font-size:13px'>{escape(state)}（{len(result.groups.get(state) or [])}）</h3>"
         + _group_table(result, result.groups.get(state) or [], 30) for state in STATE_GROUPS)}

<h2>D 精选与买点 <span class="en">PICKS</span></h2>
{_picks(result)}

<h2>E 逐只结构与计划 <span class="en">PLAN</span></h2>
{_plans(result)}
{_structure(result)}

<h2>F 风险提示 <span class="en">RISK</span></h2>
<div class="warn">
① 封单额 / 开板次数 / 首封时间 / 分时三波 / 人线：本地日线不可得，一律标「不可验证」，不用日线冒充；<br>
② 筹码是<b>日线量价估算</b>，不是真实持仓分布，不能说成机构成本；<br>
③ 阶梯与凹间峰是<b>代理指标</b>，不等于人工划线的黄金阶梯；<br>
④ 价格为不复权，除权除息日前后请人工核对；<br>
⑤ 15:00 前当日 K 线未走完，回撤比、缩量、买点都可能变；<br>
⑥ 机械复现 ≠ 战法本身：日线规则只覆盖可量化部分，最终由人判断。
</div>

<footer>
  本页由「寻龙盯盘」生成，仅用于研究与复盘，不构成投资建议，不自动下单。<br>
  数据来源：本地通达信 vipdoc 日线（{escape(str(result.source_label or ''))}）。
  涨=红 #{UP[1:]} / 跌=绿 #{DOWN[1:]}（A 股习惯）。
</footer>
</div></body></html>
"""
