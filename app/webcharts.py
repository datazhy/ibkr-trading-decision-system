"""曲线 SVG 渲染（无 JS 依赖，悬停走原生 <title>）。"""
from __future__ import annotations

import csv
import io
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from . import pnl as pnl_calc
from . import reconcile
from .config import Config
from .counting import load_decisions, trading_counts
from .decisions import decision_key
from .decisions import (
    classification_complete as classification_complete_fn,
    group_decisions, kind_summary as kind_summary_fn,
)
from .db import Database, to_decimal
from .sectors import load_sector_map

from .webformat import DISPLAY_TZ

#: 相邻两点净资产变化超过此比例且当日有资金流 -> 断线，不用一条线连起来假装是行情走势
_JUMP_RATIO = Decimal("0.20")
#: 相邻两点间**跳过的工作日**超过此数 -> 视为数据中断（Gateway 挂了），画成阴影区。
#: 按工作日算而不是自然日：周末本来就没有日终快照，那不是中断。
_GAP_WEEKDAYS = 1


def render_series_svg(points, *, marks=None, trades=None, alerts=None, gaps=None,
                      quality_by_date=None, width=760, height=230, pad=26,
                      pad_bottom=46, value_fmt="{:,.0f}",
                      zero_line=False) -> str:
    """曲线 SVG（无 JS 依赖，悬停走原生 <title>）。

    points: [(date, value)] 升序（美东交易日）
    marks:  资金流事件 [{date, amount, note}] -> 竖虚线 + 断线
    trades: 成交 [{date, n}]                  -> 轴上小三角
    alerts: R1/R2 触发 [{date, rules}]        -> 轴上小方块
    gaps:   数据中断 [(start_date, end_date)] -> 阴影区

    **跳变不连线**：净资产从 378 跳到 47069 是转账，用一条上扬的线连起来会被读成暴涨。
    只要相邻两点变化超过阈值就断开 —— 不要求资金流已登记（那是「不推断金额」的纪律，
    与「不把跳变画成连续走势」是两件事）。已登记的会标出入金/出金，未登记的标为
    「净资产跳变」，都不连线。
    """
    if len(points) < 2:
        return ('<svg viewBox="0 0 760 230" class="equity-chart"><text x="26" y="110" '
                'fill="currentColor" font-size="12">数据不足，暂无曲线</text></svg>')
    vals = [Decimal(str(v)) for _, v in points]
    vmin, vmax = min(vals), max(vals)
    if zero_line:
        # 每日盈亏在零上下摆动。**纵轴必须含 0**，否则一串全是负数的柱子
        # 看起来会像在「上涨」—— 相对最低点确实在涨，但那不是用户要读的东西。
        vmin, vmax = min(vmin, Decimal("0")), max(vmax, Decimal("0"))
    span = (vmax - vmin) or Decimal("1")
    n = len(points)
    inner_w = width - pad - 12
    inner_h = height - pad - pad_bottom

    def x(i): return pad + inner_w * i / (n - 1)
    def y(v): return pad + inner_h * float((vmax - v) / span)

    base_y = pad + inner_h
    zero_y = y(Decimal("0")) if zero_line else None
    idx = {d: i for i, (d, _) in enumerate(points)}
    flow_dates = {m["date"] for m in (marks or [])}

    # 分段：跳变处断开。一天内净资产变化超过 _JUMP_RATIO 几乎必然是资金流，
    # 把它画成连续曲线就是在讲一个假的收益故事。
    # **例外**：跨越周末/长假的相邻两点（跳过 <=1 个工作日）不因数值跳变断线 ——
    # 周一相对周五的正常涨跌不该被当成资金流。周末本就没有交易，曲线必须连贯。
    jump_dates = []
    segments, cur = [], [0]
    for i in range(1, n):
        prev_v, cur_v = vals[i - 1], vals[i]
        jumped = (prev_v > 0 and abs(cur_v - prev_v) / prev_v > _JUMP_RATIO)
        # 跨周末/假期：日历跨度 >1 天但工作日跨度 <=1 天
        d0, d1 = points[i - 1][0], points[i][0]
        if "-" in d0 and "-" in d1:
            try:
                dd0, dd1 = date.fromisoformat(d0), date.fromisoformat(d1)
                if (dd1 - dd0).days > 1 and _weekdays_between(dd0, dd1) <= 1:
                    jumped = False    # 跨周末，不断线
            except ValueError:
                pass                  # 非法日期（不应发生于真实数据）时保守处理
        if jumped:
            segments.append(cur)
            cur = [i]
            if points[i][0] not in flow_dates:
                jump_dates.append(i)      # 未登记资金流的跳变，也要标出来
        else:
            cur.append(i)
    segments.append(cur)

    color = "var(--profit)" if vals[-1] >= vals[0] else "var(--loss)"
    lines = ""
    for seg in segments:
        if len(seg) < 2:
            i = seg[0]
            lines += (f'<circle cx="{x(i):.1f}" cy="{y(vals[i]):.1f}" r="2.5" '
                      f'fill="{color}"/>')
            continue
        pts = " ".join(f"{x(i):.1f},{y(vals[i]):.1f}" for i in seg)
        lines += f'<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/>'

    # 数据中断阴影
    gap_shade = ""
    for gs, ge in (gaps or []):
        i0, i1 = idx.get(gs), idx.get(ge)
        if i0 is None or i1 is None:
            continue
        x0, x1 = x(i0), x(i1)
        gap_shade += (
            f'<rect x="{x0:.1f}" y="{pad}" width="{max(x1 - x0, 2):.1f}" '
            f'height="{inner_h:.1f}" fill="var(--warn)" opacity="0.10">'
            f'<title>数据中断：{gs} → {ge}（Gateway 未采集）</title></rect>'
        )

    # 竖线标记：已登记的资金流标「入金/出金」，未登记的跳变标「跳变」——
    # 都不连线，但绝不替用户猜金额。
    flow_marks = ""
    for i in jump_dates:
        jx = x(i)
        flow_marks += (
            f'<line x1="{jx:.1f}" y1="{pad}" x2="{jx:.1f}" y2="{base_y:.1f}" '
            f'stroke="var(--warn)" stroke-width="1" stroke-dasharray="2,3">'
            f'<title>{points[i][0]} 净资产跳变（可能是资金流，尚未登记）</title></line>'
            f'<text x="{jx:.1f}" y="{pad - 8:.1f}" class="chart-label" '
            f'text-anchor="middle" fill="var(--warn)">跳变</text>'
        )
    for m in (marks or []):
        i = idx.get(m["date"])
        if i is None:
            continue
        fx = x(i)
        sign = "入金" if m["amount"] > 0 else "出金"
        flow_marks += (
            f'<line x1="{fx:.1f}" y1="{pad}" x2="{fx:.1f}" y2="{base_y:.1f}" '
            f'stroke="var(--muted)" stroke-width="1" stroke-dasharray="3,3">'
            f'<title>{m["date"]} {sign} {m["amount"]:,.2f}（{m.get("note") or ""}）</title></line>'
            f'<text x="{fx:.1f}" y="{pad - 8:.1f}" class="chart-label" '
            f'text-anchor="middle">{sign}</text>'
        )

    # 成交标记（轴上三角）
    trade_marks = ""
    for t in (trades or []):
        i = idx.get(t["date"])
        if i is None:
            continue
        tx = x(i)
        trade_marks += (
            f'<polygon points="{tx-3.5:.1f},{base_y+9:.1f} {tx+3.5:.1f},{base_y+9:.1f} '
            f'{tx:.1f},{base_y+3:.1f}" fill="var(--accent)">'
            f'<title>{t["date"]} 成交 {t["n"]} 笔</title></polygon>'
        )

    # R1/R2 触发标记（轴上方块）
    alert_marks = ""
    for a in (alerts or []):
        i = idx.get(a["date"])
        if i is None:
            continue
        ax = x(i)
        alert_marks += (
            f'<rect x="{ax-3:.1f}" y="{base_y+4:.1f}" width="6" height="6" '
            f'fill="var(--crit)" opacity="0.85">'
            f'<title>{a["date"]} 触发 {a["rules"]}</title></rect>'
        )

    # 数据点 + 悬停（金额 / 时间 / 数据质量）
    dots = ""
    for i, (d, v) in enumerate(points):
        q = (quality_by_date or {}).get(d, "")
        tip = f"{d}\n{value_fmt.format(vals[i])}"
        if q:
            tip += f"\n数据质量：{q}"
        dots += (f'<circle cx="{x(i):.1f}" cy="{y(vals[i]):.1f}" r="7" fill="transparent">'
                 f'<title>{tip}</title></circle>')

    # x 轴日期刻度（最多 5 个，含首尾）
    n_ticks = min(5, n)
    idxs = (sorted({round(i * (n - 1) / (n_ticks - 1)) for i in range(n_ticks)})
            if n_ticks > 1 else [0])
    ticks = ""
    for i in idxs:
        tx = x(i)
        anchor = "start" if i == 0 else ("end" if i == n - 1 else "middle")
        # 日级标签取 MM-DD；盘中标签本身就是 HH:MM，原样用
        lab = points[i][0][5:] if "-" in points[i][0] else points[i][0]
        ticks += (
            f'<line x1="{tx:.1f}" y1="{base_y:.1f}" x2="{tx:.1f}" y2="{base_y + 4:.1f}" '
            f'class="chart-axis"/>'
            f'<text x="{tx:.1f}" y="{base_y + 26:.1f}" class="chart-label" '
            f'text-anchor="{anchor}">{lab}</text>'
        )

    # 年份只在日级曲线上补一次（盘中曲线的标签是 HH:MM，没有年份可言）
    year_note = (f'<text x="{pad}" y="{height - 6}" class="chart-label">'
                 f'{points[0][0][:4]}</text>') if "-" in points[0][0] else ""

    # 零轴：每日盈亏必须能一眼分出赚和亏
    zero_axis = (
        f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width - 12}" y2="{zero_y:.1f}" '
        f'class="chart-zero" stroke-dasharray="3 3"/>'
        f'<text x="{width - 12}" y="{zero_y - 3:.1f}" class="chart-label" '
        f'text-anchor="end">0</text>') if zero_y is not None else ""
    return (
        f'<svg viewBox="0 0 {width} {height}" class="equity-chart" '
        f'preserveAspectRatio="none" role="img" aria-label="曲线">'
        f'{gap_shade}'
        f'<line x1="{pad}" y1="{base_y:.1f}" x2="{width - 12}" y2="{base_y:.1f}" class="chart-axis"/>'
        f'{zero_axis}'
        f'{lines}{flow_marks}{trade_marks}{alert_marks}{dots}{ticks}'
        f'{year_note}'
        f'<text x="{pad}" y="{pad - 8}" class="chart-label">{value_fmt.format(vmax)}</text>'
        f'<text x="{pad}" y="{base_y - 4:.1f}" class="chart-label">{value_fmt.format(vmin)}</text>'
        f'<text x="{width - 12}" y="{pad - 8}" class="chart-label" text-anchor="end">'
        f'{points[-1][0]} · {value_fmt.format(vals[-1])}</text>'
        f'</svg>'
    )


def render_equity_svg(points, width=760, height=230, pad=26, pad_bottom=46, marks=None) -> str:
    """向后兼容薄封装。"""
    return render_series_svg(points, marks=marks, width=width, height=height,
                             pad=pad, pad_bottom=pad_bottom)


def _weekdays_between(d0: date, d1: date) -> int:
    """(d0, d1) 之间跳过的工作日数（不含两端）。"""
    n, cur = 0, d0 + timedelta(days=1)
    while cur < d1:
        if cur.weekday() < 5:
            n += 1
        cur += timedelta(days=1)
    return n


def find_gaps(points, max_weekdays: int = _GAP_WEEKDAYS) -> list[tuple[str, str]]:
    """相邻日终快照之间漏了太多**工作日** -> 数据中断区（Gateway 挂了那几天）。

    按工作日判定：周五到周一自然日差 3 天，但中间没有交易日，那是周末不是中断。
    """
    out = []
    for i in range(1, len(points)):
        d0, d1 = date.fromisoformat(points[i - 1][0]), date.fromisoformat(points[i][0])
        if _weekdays_between(d0, d1) > max_weekdays:
            out.append((points[i - 1][0], points[i][0]))
    return out
