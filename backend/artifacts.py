"""Reports ("artifacts") that Genesis AI can create: a titled page of KPIs, charts, tables and
suggestions in the HerVpod look, saved so the founder can open, download or print it.

The model never writes HTML. It sends structured blocks and this module renders them, so
numbers come through exactly, every report looks the same, and nothing a model (or a web
page it read) says can inject markup or scripts. Everything is escaped, and the pages are
served with a strict Content-Security-Policy (no scripts at all).

Block types (see create_artifact in llm.py for the exact fields):
  heading, text, kpis, bar_chart, line_chart, table, callout, suggestions
"""
import html
import math
import re
import secrets
from datetime import datetime
from pathlib import Path

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,40}$")
MAX_FILES = 200
MAX_BLOCKS = 30
MAX_ROWS = 200
MAX_LABELS = 24
MAX_SERIES = 3
SERIES_CLASSES = ("s1", "s2", "s3")

esc = html.escape
BULLET = re.compile(r"^[-•*]\s+")


class ArtifactError(ValueError):
    """A problem with the report spec, worded so the model can fix it and try again."""


# ---------------------------------------------------------------- helpers

def _text(value, field, max_len=2000, required=False) -> str:
    if value is None or value == "":
        if required:
            raise ArtifactError(f"'{field}' is required.")
        return ""
    return str(value).strip()[:max_len]


def _num(value, field) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ArtifactError(f"'{field}' must contain numbers only (got {value!r}).")
    if math.isnan(v) or math.isinf(v):
        raise ArtifactError(f"'{field}' contains an invalid number.")
    return v


def _inline(text: str) -> str:
    """Escape, then allow **bold** only."""
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", esc(text))


def fmt(v: float, unit: str = "") -> str:
    """Readable number: thousands commas, sensible decimals, optional $ / % / s unit."""
    if unit == "$":
        return f"${v:,.0f}" if abs(v) >= 100 else f"${v:,.2f}"
    s = f"{v:,.0f}" if (abs(v) >= 100 or v == int(v)) else f"{v:,.1f}"
    if unit in ("%", "s", "x"):
        return s + unit
    return f"{s} {unit}" if unit else s


def _nice_step(raw: float) -> float:
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    base = raw / 10 ** exp
    nice = 1 if base <= 1 else 2 if base <= 2 else 2.5 if base <= 2.5 else 5 if base <= 5 else 10
    return nice * 10 ** exp


def _series(block: dict, n_labels: int, allow_negative: bool) -> list[dict]:
    raw = block.get("series")
    if not raw and "data" in block:  # shorthand: data=[{label, value}]
        raise ArtifactError("Use 'labels' and 'series' (e.g. series=[{name, values:[...]}]).")
    if not isinstance(raw, list) or not raw:
        raise ArtifactError("Charts need 'series': [{name, values:[...]}].")
    if len(raw) > MAX_SERIES:
        raise ArtifactError(f"At most {MAX_SERIES} series per chart; split it into two charts.")
    out = []
    for i, s in enumerate(raw):
        values = [_num(v, "values") for v in (s.get("values") or [])]
        if len(values) != n_labels:
            raise ArtifactError(f"Series '{s.get('name', i + 1)}' has {len(values)} values but there are {n_labels} labels.")
        if not allow_negative and any(v < 0 for v in values):
            raise ArtifactError("Bar charts can't show negative values; use a table.")
        out.append({"name": _text(s.get("name"), "series name", 60) or f"Series {i + 1}", "values": values})
    return out


def _labels(block: dict, max_labels=MAX_LABELS) -> list[str]:
    labels = block.get("labels")
    if not isinstance(labels, list) or not labels:
        raise ArtifactError("Charts need 'labels': a list of category names or dates.")
    if len(labels) > max_labels:
        raise ArtifactError(f"At most {max_labels} labels per chart; show the top {max_labels} and fold the rest into 'Other'.")
    return [_text(l, "label", 80) for l in labels]


def _chart_head(block: dict) -> str:
    title, sub = _text(block.get("title"), "title", 120), _text(block.get("subtitle"), "subtitle", 200)
    out = f'<h3 class="chart-title">{esc(title)}</h3>' if title else ""
    return out + (f'<p class="chart-sub">{esc(sub)}</p>' if sub else "")


def _legend(series: list[dict]) -> str:
    if len(series) < 2:  # one series: the title already names it
        return ""
    items = "".join(f'<li><span class="sw {SERIES_CLASSES[i]}"></span>{esc(s["name"])}</li>' for i, s in enumerate(series))
    return f'<ul class="legend">{items}</ul>'


def _table_view(labels, series, unit, first_col="") -> str:
    head = "".join(f"<th>{esc(s['name'])}</th>" for s in series)
    rows = "".join(
        f"<tr><th scope='row'>{esc(l)}</th>" + "".join(f"<td>{esc(fmt(s['values'][i], unit))}</td>" for s in series) + "</tr>"
        for i, l in enumerate(labels))
    return (f'<details class="tableview"><summary>View as table</summary><div class="tablewrap"><table>'
            f'<thead><tr><th>{esc(first_col)}</th>{head}</tr></thead><tbody>{rows}</tbody></table></div></details>')


# ---------------------------------------------------------------- blocks

def _heading(b):
    level = 3 if b.get("level") == 3 else 2
    return f"<h{level} class='sec'>{esc(_text(b.get('text'), 'heading text', 160, True))}</h{level}>"


def _paragraphs(b):
    raw = _text(b.get("text"), "text", 4000, True)
    out = []
    for chunk in re.split(r"\n\s*\n", raw):
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]
        if lines and all(BULLET.match(l) for l in lines):
            items = [BULLET.sub("", l) for l in lines]
            out.append("<ul class='bullets'>" + "".join("<li>" + _inline(i) + "</li>" for i in items) + "</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(lines))}</p>")
    return "".join(out)


def _kpis(b):
    items = b.get("items")
    if not isinstance(items, list) or not items:
        raise ArtifactError("kpis needs 'items': [{label, value, delta?, tone?}].")
    tiles = []
    for it in items[:6]:
        label, value = _text(it.get("label"), "kpi label", 60, True), _text(it.get("value"), "kpi value", 30, True)
        delta, tone = _text(it.get("delta"), "kpi delta", 60), it.get("tone", "neutral")
        d = ""
        if delta:
            icon = "▲" if delta[0] == "+" else "▼" if delta[0] in "-−" else "●"
            tone = tone if tone in ("good", "bad", "neutral") else "neutral"
            d = f'<p class="delta {tone}"><span aria-hidden="true">{icon}</span> {esc(delta)}</p>'
        tiles.append(f'<div class="kpi"><p class="kpi-label">{esc(label)}</p><p class="kpi-value">{esc(value)}</p>{d}</div>')
    return f'<div class="kpis">{"".join(tiles)}</div>'


def _bar_chart(b):
    labels, unit = _labels(b), _text(b.get("unit"), "unit", 12)
    series = _series(b, len(labels), allow_negative=False)
    peak = max((v for s in series for v in s["values"]), default=0) or 1
    rows = []
    for i, label in enumerate(labels):
        bars = ""
        for si, s in enumerate(series):
            v = s["values"][i]
            width = max(v / peak * 100, 0.8 if v > 0 else 0)
            bars += (f'<div class="bar-line"><div class="bar {SERIES_CLASSES[si]}" style="width:{width:.2f}%"></div>'
                     f'<span class="bar-val">{esc(fmt(v, unit))}</span></div>')
        rows.append(f'<div class="bar-row"><div class="bar-label" title="{esc(label)}">{esc(label)}</div><div class="bar-group">{bars}</div></div>')
    return (f'<figure class="card chart">{_chart_head(b)}{_legend(series)}<div class="bars">{"".join(rows)}</div>'
            f'{_table_view(labels, series, unit)}</figure>')


def _line_svg(labels, series, unit, W, H, max_x_labels, cls) -> str:
    all_vals = [v for s in series for v in s["values"]]
    lo, hi = min(0.0, min(all_vals)), max(all_vals) or 1.0
    step = _nice_step((hi - lo) / 4 or 1)
    top = math.ceil(hi / step) * step
    bottom = math.floor(lo / step) * step
    ticks = [bottom + i * step for i in range(int(round((top - bottom) / step)) + 1)]
    tick_txt = [(f"${t:,.0f}" if unit == "$" else fmt(t, unit)) for t in ticks]
    left = max(len(t) for t in tick_txt) * 7 + 14
    right, pad_top, pad_bottom = 52, 12, 28
    pw, ph = W - left - right, H - pad_top - pad_bottom
    n = len(labels)

    def x(i):
        return left + (pw * i / (n - 1) if n > 1 else pw / 2)

    def y(v):
        return pad_top + ph * (1 - (v - bottom) / ((top - bottom) or 1))

    out = [f'<svg class="{cls}" viewBox="0 0 {W} {H}" role="img" aria-label="{esc("Line chart: " + ", ".join(s["name"] for s in series))}">']
    for t, txt in zip(ticks, tick_txt):
        out.append(f'<line class="grid" x1="{left}" x2="{W - right}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="axis" x="{left - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(txt)}</text>')
    shown = sorted({0, n - 1, *[round(i * (n - 1) / (max_x_labels - 1)) for i in range(max_x_labels)]}) if n > max_x_labels else range(n)
    for i in shown:
        anchor = "start" if i == 0 and n > 1 else "end" if i == n - 1 and n > 1 else "middle"
        out.append(f'<text class="axis" x="{x(i):.1f}" y="{H - 8}" text-anchor="{anchor}">{esc(labels[i])}</text>')
    end_labels = []
    for si, s in enumerate(series):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(s["values"]))
        c = SERIES_CLASSES[si]
        if len(series) == 1 and n > 1:
            area = f"{x(0):.1f},{y(bottom):.1f} {pts} {x(n - 1):.1f},{y(bottom):.1f}"
            out.append(f'<polygon class="area {c}" points="{area}"/>')
        out.append(f'<polyline class="line {c}" points="{pts}"/>')
        mark_all = n <= 10
        for i, v in enumerate(s["values"]):
            if mark_all or i == n - 1:
                out.append(f'<circle class="dot {c}" cx="{x(i):.1f}" cy="{y(v):.1f}" r="4"/>')
        for i, v in enumerate(s["values"]):  # generous invisible hit area with a native hover tooltip
            out.append(f'<circle class="hit" cx="{x(i):.1f}" cy="{y(v):.1f}" r="12"><title>{esc(s["name"])}, {esc(labels[i])}: {esc(fmt(v, unit))}</title></circle>')
        end_labels.append((y(s["values"][-1]), fmt(s["values"][-1], unit)))
    last_y = -99
    for yy, txt in sorted(end_labels):  # label the line ends, skipping any that would collide
        if yy - last_y >= 14:
            out.append(f'<text class="endval" x="{W - right + 8}" y="{yy + 4:.1f}">{esc(txt)}</text>')
            last_y = yy
    out.append("</svg>")
    return "".join(out)


def _line_chart(b):
    labels, unit = _labels(b, 60), _text(b.get("unit"), "unit", 12)
    series = _series(b, len(labels), allow_negative=True)
    wide = _line_svg(labels, series, unit, 640, 270, 8, "wide")
    narrow = _line_svg(labels, series, unit, 360, 250, 4, "narrow")
    return (f'<figure class="card chart">{_chart_head(b)}{_legend(series)}{wide}{narrow}'
            f'{_table_view(labels, series, unit)}</figure>')


def _table(b):
    cols, rows = b.get("columns"), b.get("rows")
    if not isinstance(cols, list) or not cols or not isinstance(rows, list):
        raise ArtifactError("table needs 'columns': [names] and 'rows': [[cells]].")
    if len(rows) > MAX_ROWS:
        raise ArtifactError(f"Tables are limited to {MAX_ROWS} rows.")
    numeric = re.compile(r"^[\s$€£+\-−]?[\d,]*\.?\d+\s?[%smx]?$")
    clean = []
    for r in rows:
        if not isinstance(r, list) or len(r) != len(cols):
            raise ArtifactError(f"Every table row needs exactly {len(cols)} cells.")
        clean.append([_text(c, "cell", 200) for c in r])
    is_num = [bool(clean) and all(numeric.match(r[i]) for r in clean) for i in range(len(cols))]
    head = "".join(f'<th class="{"num" if is_num[i] else ""}">{esc(_text(c, "column", 60))}</th>' for i, c in enumerate(cols))
    body = ["<tr>" + "".join(f'<td class="{"num" if is_num[i] else ""}">{esc(c)}</td>' for i, c in enumerate(r)) + "</tr>" for r in clean]
    return (f'<figure class="card chart">{_chart_head(b)}<div class="tablewrap"><table class="data">'
            f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div></figure>')


def _callout(b):
    tone = b.get("tone", "info")
    tone = tone if tone in ("info", "good", "warning", "bad") else "info"
    icon = {"info": "i", "good": "✓", "warning": "!", "bad": "✕"}[tone]
    title, text = _text(b.get("title"), "callout title", 120), _text(b.get("text"), "callout text", 1500, True)
    return (f'<aside class="callout {tone}"><span class="ico" aria-hidden="true">{icon}</span><div>'
            f'{"<p class=ctitle>" + esc(title) + "</p>" if title else ""}<p>{_inline(text)}</p></div></aside>')


def _suggestions(b):
    items = b.get("items")
    if not isinstance(items, list) or not items:
        raise ArtifactError("suggestions needs 'items': [{title, why, action, impact?, priority?}].")
    cards = []
    for it in items[:8]:
        prio = it.get("priority", "medium")
        prio = prio if prio in ("high", "medium", "low") else "medium"
        rows = [("Why", _text(it.get("why"), "why", 600)), ("What to do", _text(it.get("action"), "action", 800)),
                ("Expected effect", _text(it.get("impact"), "impact", 400))]
        dl = "".join(f"<dt>{k}</dt><dd>{_inline(v)}</dd>" for k, v in rows if v)
        cards.append(f'<article class="card sug"><div class="sug-head"><span class="prio {prio}">{prio.capitalize()} priority</span>'
                     f'<h3>{esc(_text(it.get("title"), "title", 140, True))}</h3></div><dl>{dl}</dl></article>')
    return f'<div class="sugs">{"".join(cards)}</div>'


RENDERERS = {"heading": _heading, "text": _paragraphs, "kpis": _kpis, "bar_chart": _bar_chart,
             "line_chart": _line_chart, "table": _table, "callout": _callout, "suggestions": _suggestions}


# ---------------------------------------------------------------- page

CSS = """
:root{--bg:#f8f3f6;--card:#fff;--ink:#641f40;--ink2:#7a4a62;--muted:#8d6177;--line:#eedbe5;--grid:#efe4ea;
--rose:#c24784;--blush:#faecf3;--s1:#c24784;--s2:#2a78d6;--s3:#b97a14;
--good:#1a7f4b;--bad:#c62828;--warn:#9a5a0c;--info:#2a78d6;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#1b0e15;--card:#26141e;--ink:#f7e7ef;--ink2:#dcc0cf;--muted:#b995a9;
--line:#43263a;--grid:#3a2231;--rose:#f07bb5;--blush:#3a1c2b;--s1:#d65a93;--s2:#3b84dc;--s3:#bf8a24;
--good:#5fd196;--bad:#f18a80;--warn:#eaa850;--info:#6aa8ee;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.6 Raleway,-apple-system,"Segoe UI",sans-serif}
.page{max-width:880px;margin:0 auto;padding:36px 20px 56px}
.brand{font-weight:600;letter-spacing:.5px;font-size:14px;color:var(--rose);margin:0 0 18px}
h1{font-size:32px;line-height:1.15;font-weight:500;letter-spacing:.2px;margin:0 0 6px}
.sub{font:400 15px/1.6 "Libre Baskerville",Georgia,serif;color:var(--ink2);margin:0 0 6px;max-width:64ch}
.meta{font-size:13px;color:var(--muted);margin:0 0 26px}
h2.sec{font-size:21px;font-weight:600;letter-spacing:.2px;margin:34px 0 10px}
h3.sec{font-size:17px;font-weight:600;margin:24px 0 8px}
.page p,.page li{font-family:"Libre Baskerville",Georgia,serif;font-size:14.5px;line-height:1.75}
.page p{margin:0 0 12px;max-width:68ch}
.bullets{margin:0 0 12px;padding-left:20px;max-width:68ch}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px 20px;margin:0 0 16px}
.chart{margin-left:0;margin-right:0}
.chart-title{font-size:16px;font-weight:600;margin:0 0 2px}
.chart-sub{font:400 13px/1.5 Raleway,sans-serif!important;color:var(--muted);margin:0 0 12px!important}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;list-style:none;margin:8px 0 12px;padding:0}
.legend li{font:500 13px Raleway,sans-serif!important;color:var(--ink2);display:flex;align-items:center;gap:7px}
.sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.sw.s1,.bar.s1{background:var(--s1)}.sw.s2,.bar.s2{background:var(--s2)}.sw.s3,.bar.s3{background:var(--s3)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:0 0 16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.page .kpi p{font-family:Raleway,sans-serif;margin:0}
.kpi-label{font-size:13px;color:var(--muted)}
.kpi-value{font-size:28px!important;font-weight:600;line-height:1.2;letter-spacing:-.2px;margin:2px 0 2px!important}
.delta{font-size:12.5px!important;font-weight:600}
.delta.good{color:var(--good)}.delta.bad{color:var(--bad)}.delta.neutral{color:var(--muted)}
.bars{display:grid;gap:12px}
.bar-row{display:grid;grid-template-columns:minmax(80px,32%) 1fr;gap:12px;align-items:center}
.bar-label{font:500 13.5px Raleway,sans-serif;color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bar-group{display:grid;gap:2px;border-left:1px solid var(--grid)}
.bar-line{display:flex;align-items:center;gap:8px;min-height:20px}
.bar{height:18px;border-radius:0 4px 4px 0;min-width:2px}
.bar-val{font:600 13px Raleway,sans-serif;color:var(--ink);white-space:nowrap}
svg{width:100%;height:auto;display:block;overflow:visible}
svg.narrow{display:none}
svg .grid{stroke:var(--grid);stroke-width:1}
svg .axis{fill:var(--muted);font:12px Raleway,sans-serif}
svg .endval{fill:var(--ink);font:600 12.5px Raleway,sans-serif}
svg .line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
svg .line.s1{stroke:var(--s1)}svg .line.s2{stroke:var(--s2)}svg .line.s3{stroke:var(--s3)}
svg .area{stroke:none;opacity:.1}
svg .area.s1,svg .dot.s1{fill:var(--s1)}svg .area.s2,svg .dot.s2{fill:var(--s2)}svg .area.s3,svg .dot.s3{fill:var(--s3)}
svg .dot{stroke:var(--card);stroke-width:2}
svg .hit{fill:transparent}
.tableview{margin-top:12px;font:500 13px Raleway,sans-serif}
.tableview summary{cursor:pointer;color:var(--ink2);padding:4px 0}
.tablewrap{overflow-x:auto;margin-top:6px}
table{border-collapse:collapse;width:100%;font:13.5px/1.5 Raleway,sans-serif}
th,td{text-align:left;padding:8px 12px 8px 0;border-bottom:1px solid var(--grid);white-space:nowrap}
thead th{font-weight:600;color:var(--ink2)}tbody th{font-weight:500}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
table.data th:last-child,table.data td:last-child{padding-right:0}
table.data td{white-space:normal}
.callout{display:flex;gap:12px;border-radius:10px;padding:14px 16px;margin:0 0 16px;background:var(--blush);border:1px solid var(--line)}
.callout p{margin:0!important}.callout .ctitle{font:600 14.5px Raleway,sans-serif;margin-bottom:2px!important}
.ico{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font:700 13px Raleway,sans-serif;color:#fff;margin-top:1px}
.callout.info .ico{background:var(--info)}.callout.good .ico{background:var(--good)}
.callout.warning .ico{background:var(--warn)}.callout.bad .ico{background:var(--bad)}
.sugs{display:grid;gap:14px;margin:0 0 16px}
.sug{margin:0}
.sug-head h3{font-size:17px;font-weight:600;margin:6px 0 8px}
.prio{display:inline-block;font:600 12px Raleway,sans-serif;padding:2px 9px;border-radius:999px;border:1px solid currentColor}
.prio.high{color:var(--bad)}.prio.medium{color:var(--warn)}.prio.low{color:var(--muted)}
.sug dl{margin:0;display:grid;grid-template-columns:max-content 1fr;gap:6px 16px}
.sug dt{font:600 13px Raleway,sans-serif;color:var(--ink2);padding-top:2px}
.sug dd{margin:0;font:14px/1.65 "Libre Baskerville",Georgia,serif}
footer{margin-top:34px;padding-top:14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
@media (max-width:620px){
 .page{padding:24px 16px 44px}h1{font-size:26px}
 svg.wide{display:none}svg.narrow{display:block}
 .bar-row{grid-template-columns:1fr;gap:4px}.bar-label{white-space:normal}
 .sug dl{grid-template-columns:1fr;gap:2px}.sug dt{padding-top:8px}.card{padding:16px}
}
@media print{body{background:#fff}.card,.kpi,.callout{break-inside:avoid}.tableview{display:none}}
"""

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Libre+Baskerville:wght@400;700&family=Raleway:wght@400;500;600&display=swap" rel="stylesheet">')

CSP = ("default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
       "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")


def render_page(spec: dict, when: datetime | None = None) -> str:
    title = _text(spec.get("title"), "title", 140, True)
    subtitle = _text(spec.get("subtitle"), "subtitle", 300)
    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ArtifactError("'blocks' must be a non-empty list.")
    if len(blocks) > MAX_BLOCKS:
        raise ArtifactError(f"At most {MAX_BLOCKS} blocks per report.")
    body = []
    for i, b in enumerate(blocks, 1):
        kind = b.get("type") if isinstance(b, dict) else None
        if kind not in RENDERERS:
            raise ArtifactError(f"Block {i}: unknown type {kind!r}. Use one of: {', '.join(RENDERERS)}.")
        try:
            body.append(RENDERERS[kind](b))
        except ArtifactError as e:
            raise ArtifactError(f"Block {i} ({kind}): {e}")
    sources = spec.get("sources")
    src = ""
    if isinstance(sources, list) and sources:
        src = "Sources: " + esc(", ".join(_text(s, "source", 80) for s in sources[:8])) + ". "
    when = when or datetime.now()
    created = f"{when:%A, %B} {when.day} at {when:%-I:%M %p}"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{esc(title)} · Genesis AI</title>{FONTS}<style>{CSS}</style></head><body><main class="page">'
            f'<p class="brand">HerVpod · Genesis AI</p><h1>{esc(title)}</h1>'
            f'{"<p class=sub>" + esc(subtitle) + "</p>" if subtitle else ""}<p class="meta">Created {created}</p>'
            f'{"".join(body)}<footer>{src}Numbers come straight from Shopify and Clarity when this report was created; '
            f'they won\'t update on their own.</footer></main></body></html>')


# ---------------------------------------------------------------- storage

def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "report"


def filename_for(page: str) -> str:
    """Download name taken from the report's <title>."""
    m = re.search(r"<title>(.*?) · Genesis AI</title>", page)
    return f"{slugify(html.unescape(m.group(1))) if m else 'report'}.html"


def create_artifact(spec: dict) -> dict:
    page = render_page(spec)
    ARTIFACT_DIR.mkdir(exist_ok=True)
    art_id = secrets.token_urlsafe(9)
    (ARTIFACT_DIR / f"{art_id}.html").write_text(page, encoding="utf-8")
    old = sorted(ARTIFACT_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime)[:-MAX_FILES]
    for p in old:
        p.unlink(missing_ok=True)
    title = _text(spec.get("title"), "title", 140)
    return {"id": art_id, "title": title, "url": f"/api/artifacts/{art_id}", "filename": f"{slugify(title)}.html"}


def read_artifact(art_id: str) -> str | None:
    if not ID_RE.match(art_id or ""):
        return None
    path = ARTIFACT_DIR / f"{art_id}.html"
    return path.read_text(encoding="utf-8") if path.is_file() else None
