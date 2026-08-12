"""历史重放页面数据。

一条压倒一切的展示纪律：**页面必须让人一眼看出这不是真实告警**。
模拟事件被误读成"当时系统报过警"，会让整个风险时间线失去可信度。
所以顶部固定一条声明，每张表也各自标注模式。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import replay_identity as rid
from . import replay_service as rp
from .logging_setup import get_logger

log = get_logger()

DISCLAIMER = ("历史重放结果不是当时实际发送过的告警。"
              "它是用规则在历史快照上重新计算的模拟结果，"
              "未发送任何通知，也未写入实时风险事件时间线。")

MODE_LABEL = {
    rp.MODE_POINT_IN_TIME: "point-in-time 重建（用当时的规则版本）",
    rp.MODE_CURRENT_RULES: "current-rules 模拟（用今天的规则跑历史）",
}
SAMPLE_LABEL = {rp.SAMPLE_DAILY_CLOSE: "每日收盘", rp.SAMPLE_INTRADAY: "盘中按间隔"}
STATUS_LABEL = {rp.ST_PENDING: "待运行", rp.ST_RUNNING: "运行中",
                rp.ST_SUCCESS: "成功", rp.ST_PARTIAL: "部分完成",
                rp.ST_FAILED: "失败"}
IDENTITY_LABEL = {
    rid.ID_NATIVE: "原生 snapshot_id",
    rid.ID_RECONSTRUCTED: "时间戳重建",
    rid.ID_INSUFFICIENT: "无法关联",
}
ALIGN_LABEL = {rid.ALIGN_EXACT: "精确", rid.ALIGN_TOLERANCE: "容差内",
               rid.ALIGN_ESTIMATED: "估算", rid.ALIGN_INVALID: "无效"}


def _jload(v, default):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


def page_data(cfg, db, now: datetime | None = None, *,
              run_id: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    tz = cfg.local_tz
    from .webformat import abs_time

    try:
        coverage = rp.coverage_report(cfg, db)
    except Exception:  # noqa: BLE001
        log.warning("重放覆盖率读取失败", exc_info=True)
        coverage = {}

    runs = []
    try:
        for r in db.replay_runs(limit=20):
            runs.append({
                **r,
                "status_label": STATUS_LABEL.get(r.get("status"), r.get("status")),
                "mode_label": MODE_LABEL.get(r.get("rule_set_mode"),
                                             r.get("rule_set_mode")),
                "sample_label": SAMPLE_LABEL.get(r.get("sampling_mode"),
                                                 r.get("sampling_mode")),
                "started": abs_time(r.get("started_at"), tz),
                "coverage_summary": _jload(r.get("coverage_summary"), {}),
                "skip_reasons": _jload(r.get("skip_reasons"), {}),
            })
    except Exception:  # noqa: BLE001
        runs = []

    selected = None
    events: list[dict] = []
    rule_summary: list[dict] = []
    if run_id or runs:
        rid_ = run_id or runs[0]["replay_run_id"]
        try:
            selected = db.get_replay_run(rid_)
            if selected:
                selected = {
                    **selected,
                    "status_label": STATUS_LABEL.get(selected.get("status")),
                    "mode_label": MODE_LABEL.get(selected.get("rule_set_mode")),
                    "sample_label": SAMPLE_LABEL.get(selected.get("sampling_mode")),
                    "coverage_summary": _jload(selected.get("coverage_summary"), {}),
                    "skip_reasons": _jload(selected.get("skip_reasons"), {}),
                }
                rows, _ = db.replay_events(rid_, limit=100)
                for e in rows:
                    events.append({
                        **e,
                        "first_abs": abs_time(e.get("first_detected_at"), tz),
                        "resolved_abs": (abs_time(e.get("resolved_at"), tz)
                                         if e.get("resolved_at") else None),
                        "reliable_label": "可靠" if e.get("reliable") else "存疑",
                        "matched_label": ("与真实事件重合"
                                          if e.get("matched_real_event_id")
                                          else "无匹配真实事件"),
                    })
                rule_summary = db.replay_rule_summary(rid_)
        except Exception:  # noqa: BLE001
            log.warning("重放详情读取失败", exc_info=True)

    return {
        "disclaimer": DISCLAIMER,
        "coverage": coverage,
        "runs": runs,
        "selected": selected,
        "events": events,
        "rule_summary": rule_summary,
        "identity_labels": IDENTITY_LABEL,
        "align_labels": ALIGN_LABEL,
        "modes": [(rp.MODE_CURRENT_RULES, MODE_LABEL[rp.MODE_CURRENT_RULES]),
                  (rp.MODE_POINT_IN_TIME, MODE_LABEL[rp.MODE_POINT_IN_TIME])],
        "samplings": [(rp.SAMPLE_DAILY_CLOSE, SAMPLE_LABEL[rp.SAMPLE_DAILY_CLOSE]),
                      (rp.SAMPLE_INTRADAY, SAMPLE_LABEL[rp.SAMPLE_INTRADAY])],
    }
