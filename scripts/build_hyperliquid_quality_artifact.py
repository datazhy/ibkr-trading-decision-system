"""把服务器生成的完整性JSON快照打包为Data Analytics标准报告artifact。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _pct(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_artifact(report: dict[str, Any]) -> dict[str, Any]:
    summary = report["summary"]
    generated = summary["generated_at"]
    checks = report.get("checks", [])
    non_pass = [row for row in checks if row.get("status") != "PASS"]
    readiness = [{**row, "coverage_pct": 100 * _pct(row.get("coverage_ratio"))}
                 for row in report.get("readiness", [])]
    progress = report.get("progress", [])
    wallet_rows = sorted(report.get("wallets", []), key=lambda row: (
        bool(row.get("complete")), -int(row.get("fills") or 0)))
    raw_equity_rows = sorted(report.get("equity", []), key=lambda row: (
        bool(row.get("complete")), -_pct(row.get("coverage_ratio"))))
    equity_rows = [{
        "symbol": row.get("symbol"), "rows": row.get("rows"),
        "sessions": row.get("sessions"), "min_time": row.get("min_time"),
        "max_time": row.get("max_time"), "expected_bars": row.get("expected_bars"),
        "coverage_ratio": row.get("coverage_ratio"), "complete": row.get("complete"),
        "source_rows_text": json.dumps(row.get("source_rows", {}), ensure_ascii=False,
                                       sort_keys=True),
    } for row in raw_equity_rows]

    source = {
        "id": "quality_snapshot",
        "label": "Hyperliquid 历史数据完整性快照",
        "path": "data/hyperliquid-reports/latest.json",
        "description": "由hyperliquid.db与dashboard.db只读检查生成的不可变JSON快照。",
        "query": {
            "engine": "sqlite",
            "language": "sql",
            "sql": ("SELECT report_id, generated_at, requested_start, requested_end, "
                    "report_version, status, summary_json, checks_json, report_path "
                    "FROM hl_data_quality_reports ORDER BY generated_at DESC LIMIT 1"),
            "description": ("定位最新的持久化质量报告；图表明细由该报告ID对应的"
                            "latest.json确定性展开。"),
            "tables_used": ["hyperliquid.hl_data_quality_reports"],
            "executed_at": generated,
            "filters": ["最新generated_at的一条报告快照"],
            "metric_definitions": {
                "coverage_ratio": "完整实体数除以当前报告要求的实体总数。",
                "complete_wallets": "fills/funding/ledger游标均完整且无截断的钱包数。",
                "complete_symbols": "唯一RTH 5分钟bar覆盖达到预期90%的标的数。",
            },
        },
    }
    cards = [
        {
            "id": "status_card", "description": "当前能否创建锁定测试承诺。",
            "dataset": "headline", "sourceId": "quality_snapshot",
            "metrics": [{"label": "当前状态", "field": "status", "format": "text"}],
        },
        {
            "id": "wallet_card", "description": "三账本游标均完整结束且无截断的钱包。",
            "dataset": "headline", "sourceId": "quality_snapshot",
            "metrics": [{"label": "完整钱包", "field": "complete_wallets", "format": "number"},
                        {"label": "跟踪钱包", "field": "tracked_wallets", "format": "number"}],
        },
        {
            "id": "equity_card", "description": "达到90%预期RTH 5分钟bar覆盖的现实标的。",
            "dataset": "headline", "sourceId": "quality_snapshot",
            "metrics": [{"label": "完整正股标的", "field": "complete_symbols", "format": "number"},
                        {"label": "必需标的", "field": "required_symbols", "format": "number"}],
        },
        {
            "id": "blocker_card", "description": "必须修复后才能开始锁定测试的关键门禁。",
            "dataset": "headline", "sourceId": "quality_snapshot",
            "metrics": [{"label": "关键阻断项", "field": "blocking_count", "format": "number"}],
        },
    ]
    charts = [
        {
            "id": "readiness_chart", "title": "关键数据组件覆盖率",
            "subtitle": "请求窗口内的实体覆盖；90%是进入锁定测试的最低目标",
            "type": "bar", "dataset": "readiness", "sourceId": "quality_snapshot",
            "valueFormat": "number", "width": 10,
            "encodings": {
                "x": {"field": "component", "type": "nominal", "label": "数据组件"},
                "y": {"field": "coverage_pct", "type": "quantitative", "label": "覆盖率（%）"},
                "tooltip": [
                    {"field": "complete", "type": "quantitative", "label": "完整实体"},
                    {"field": "total", "type": "quantitative", "label": "总实体"},
                    {"field": "status", "type": "nominal", "label": "门禁状态"},
                ],
            },
        },
        {
            "id": "progress_chart", "title": "历史回溯累计写入量",
            "subtitle": "按回溯步骤时间排序；不同数据集的行粒度不同，仅用于确认流水线持续推进",
            "type": "line", "dataset": "progress", "sourceId": "quality_snapshot",
            "valueFormat": "number", "width": 10,
            "encodings": {
                "x": {"field": "started_at", "type": "temporal", "label": "步骤开始时间"},
                "y": {"field": "cumulative_rows", "type": "quantitative", "label": "累计写入行数"},
                "tooltip": [
                    {"field": "scope", "type": "nominal", "label": "范围"},
                    {"field": "rows_written", "type": "quantitative", "label": "本步写入"},
                    {"field": "status", "type": "nominal", "label": "状态"},
                ],
            },
        },
    ]
    tables = [
        {
            "id": "check_table", "title": "未通过检查明细",
            "subtitle": "每项均给出严重级别、当前值、风险和最小修复动作",
            "dataset": "non_pass_checks", "sourceId": "quality_snapshot",
            "defaultSort": {"field": "severity", "direction": "asc"},
            "columns": [
                {"field": "id", "label": "检查", "type": "text"},
                {"field": "status", "label": "状态", "type": "text"},
                {"field": "severity", "label": "严重级别", "type": "text"},
                {"field": "observed_text", "label": "当前值", "type": "text"},
                {"field": "threshold", "label": "门槛", "type": "text"},
                {"field": "remediation", "label": "修复动作", "type": "text"},
            ],
        },
        {
            "id": "wallet_table", "title": "跟踪钱包历史覆盖",
            "subtitle": "按完整状态和fill数量排序；地址只显示前后缀",
            "dataset": "wallets", "sourceId": "quality_snapshot",
            "defaultSort": {"field": "fills", "direction": "desc"},
            "columns": [
                {"field": "wallet", "label": "钱包", "type": "text"},
                {"field": "fills", "label": "Fills", "format": "number"},
                {"field": "funding", "label": "Funding", "format": "number"},
                {"field": "ledger", "label": "Ledger", "format": "number"},
                {"field": "fills_pages", "label": "Fill页数", "format": "number"},
                {"field": "complete", "label": "完整", "type": "boolean"},
                {"field": "incomplete", "label": "截断/缺口", "type": "boolean"},
            ],
        },
        {
            "id": "equity_table", "title": "正股5分钟bar覆盖",
            "subtitle": "IBKR优先、富途补缺；同一bar_start去重后计算覆盖",
            "dataset": "equity", "sourceId": "quality_snapshot",
            "defaultSort": {"field": "coverage_ratio", "direction": "asc"},
            "columns": [
                {"field": "symbol", "label": "标的", "type": "text"},
                {"field": "rows", "label": "唯一bar", "format": "number"},
                {"field": "expected_bars", "label": "预期bar", "format": "number"},
                {"field": "coverage_ratio", "label": "覆盖率", "format": "percent"},
                {"field": "min_time", "label": "最早时间", "type": "datetime"},
                {"field": "max_time", "label": "最晚时间", "type": "datetime"},
                {"field": "complete", "label": "完整", "type": "boolean"},
            ],
        },
    ]

    blockers = summary.get("blocking_check_ids", [])
    blocker_text = "、".join(f"`{item}`" for item in blockers) or "无"
    blocks = [
        {"id": "title", "type": "markdown",
         "body": "# Hyperliquid XYZ 历史数据完整性报告"},
        {"id": "technical_summary", "type": "markdown", "sourceId": "quality_snapshot",
         "body": ("## 技术摘要\n\n"
                  f"**当前结论：{summary['decision']}。** 报告状态为 `{summary['status']}`，"
                  f"关键阻断项为 {blocker_text}。自动回溯仍在推进，但当前证据不足以创建"
                  "锁定测试承诺，也不授权打开钱包评分、影子信号或交易台影响。")},
        {"id": "metrics", "type": "metric-strip",
         "cardIds": [card["id"] for card in cards]},
        {"id": "coverage_finding", "type": "markdown", "sourceId": "quality_snapshot",
         "body": ("## 历史行情已有进展，但关键链路仍未闭合\n\n"
                  "下图按实体覆盖率比较资产映射、历史ctx、历史funding/premium、"
                  "钱包三账本、成交K线和现实正股5分钟bar。"
                  "成交K线覆盖不能抵消asset ctx缺失；现实正股bar必须达到同窗覆盖才能计算2h/20d、MFE和MAE。")},
        {"id": "readiness_chart_block", "type": "chart", "chartId": "readiness_chart"},
        {"id": "pipeline_finding", "type": "markdown", "sourceId": "quality_snapshot",
         "body": ("## 回溯流水线持续推进\n\n"
                  "累计写入量用于判断任务是否停滞，不用于比较不同数据源的经济含义。"
                  "钱包fill、链上K线和正股bar粒度不同，因此斜率变化只代表作业类型变化。")},
        {"id": "progress_chart_block", "type": "chart", "chartId": "progress_chart"},
        {"id": "definitions", "type": "markdown",
         "body": ("## 范围、数据与指标定义\n\n"
                  f"评估窗口为 `{summary['requested_start']}` 至 `{summary['requested_end']}`。"
                  "完整钱包指fills、funding、ledger三个可恢复游标全部结束且没有10k截断；"
                  "完整正股标的指IBKR优先、富途补缺并按bar_start去重后，至少覆盖90%的预期NYSE RTH 5分钟bar。"
                  "`HISTORICAL_BACKFILL`只用于重建，不计入锁定测试或前瞻确认。")},
        {"id": "check_table_block", "type": "table", "tableId": "check_table"},
        {"id": "wallet_finding", "type": "markdown", "sourceId": "quality_snapshot",
         "body": ("## 钱包覆盖决定PnL能否重算\n\n"
                  "Funding或ledger合法情况下可能为零，因此不能只按行数判缺失；报告以游标完整结束为准。"
                  "任何10k截断或左删失钱包继续保持LIMITED，并从信号群体排除。")},
        {"id": "wallet_table_block", "type": "table", "tableId": "wallet_table"},
        {"id": "equity_finding", "type": "markdown", "sourceId": "quality_snapshot",
         "body": ("## 现实价格使用双源但不混源\n\n"
                  "计算时同一时点IBKR优先、富途只补空洞；数据库保留各自source。富途投入锁定样本前，"
                  "需在至少10个标的（不足10则全部）各对齐78根bar，并满足收盘价偏差p95不超过0.5%。")},
        {"id": "equity_table_block", "type": "table", "tableId": "equity_table"},
        {"id": "methods", "type": "markdown",
         "body": ("## 方法\n\n"
                  "报告分别检查完整性、唯一性、有效性、跨源一致性、时间新鲜度和PIT风险。"
                  "所有统计来自报告生成时的只读数据库快照；生成器仅向独立报告表写入摘要和检查结果。"
                  "成交K线与asset ctx分别统计，不允许用candleSnapshot替代历史mark/oracle。")},
        {"id": "limitations", "type": "markdown",
         "body": ("## 限制与稳健性\n\n"
                  "这是第一版滚动快照，回补仍在到达，因此覆盖率会继续变化。当前明确采用无AWS双轨路线："
                  "历史asset ctx不可访问使FULL_CTX轨只能前瞻积累；REDUCED_NO_CTX轨继续回溯钱包、成交价和fundingHistory。富途历史K线受7天滚动股票额度约束，"
                  "所以必须持续记录配额并由IBKR完成独立对账。报告不证明信号有效，也不改变生产开关。")},
        {"id": "next_steps", "type": "markdown",
         "body": ("## 推荐下一步\n\n"
                  "1. 继续自动分页官方fundingHistory、钱包三账本与链上成交K线，直至缩减轨关键覆盖达到90%。\n"
                  "2. 继续富途快速补齐与IBKR权威对账；同时从实时ctx前瞻积累FULL_CTX轨。\n"
                  "3. 修复或排除映射未就绪实体；只有REDUCED_NO_CTX自身的门禁变为READY后，才冻结该版本rules/policy并创建不可变承诺。")},
        {"id": "further_questions", "type": "markdown",
         "body": ("## 后续需要回答的问题\n\n"
                  "- fundingHistory逐币完成后，缩减轨实际可用的时间交集有多长？\n"
                  "- 10k截断后仍能形成多少个无左删失、可评分钱包？\n"
                  "- 富途与IBKR在拆股、提前收盘和异常交易日上的偏差是否仍低于门槛？")},
    ]
    severity_rank = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    non_pass_rows = [{
        "id": row.get("id"), "category": row.get("category"),
        "status": row.get("status"), "severity": row.get("severity"),
        "severity_rank": severity_rank.get(str(row.get("severity")), 0),
        "observed_text": json.dumps(row.get("observed"), ensure_ascii=False,
                                    sort_keys=True, default=str),
        "threshold": row.get("threshold"), "evidence": row.get("evidence"),
        "impact": row.get("impact"), "remediation": row.get("remediation"),
    } for row in non_pass]
    headline = [{
        "status": summary["status"],
        "complete_wallets": summary.get("wallets", {}).get("complete", 0),
        "tracked_wallets": summary.get("wallets", {}).get("tracked", 0),
        "complete_symbols": summary.get("equity", {}).get("complete_symbols", 0),
        "required_symbols": summary.get("equity", {}).get("required_symbols", 0),
        "blocking_count": len(blockers),
    }]
    manifest = {
        "version": 1, "surface": "report",
        "title": "Hyperliquid XYZ 历史数据完整性报告",
        "description": "判断快速通道是否具备创建锁定测试承诺的数据条件。",
        "generatedAt": generated, "cards": cards, "charts": charts, "tables": tables,
        "sources": [source], "blocks": blocks,
    }
    snapshot = {
        "version": 1, "generatedAt": generated,
        "status": "blocked" if summary["status"] == "BLOCKED" else
                  ("partial" if summary["status"] == "PARTIAL" else "ready"),
        "datasets": {
            "headline": headline, "readiness": readiness, "progress": progress,
            "non_pass_checks": non_pass_rows, "wallets": wallet_rows,
            "equity": equity_rows,
        },
        "accessIssues": ([{
            "id": "asset_ctx_requester_identity",
            "dataset": "historical_asset_ctx",
            "message": "服务器无可解析的AWS计费身份，官方requester-pays asset_ctxs当前不可访问。",
        }] if "asset_ctx_requester_identity" in blockers else []),
    }
    return {"surface": "report", "manifest": manifest, "snapshot": snapshot,
            "sources": [source]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = json.loads(Path(args.input).read_text(encoding="utf-8"))
    Path(args.output).write_text(
        json.dumps(build_artifact(report), ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
