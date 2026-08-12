"""版本化 Prompt（第二阶段）。

**AI 只被允许把已经算好的数字组织成可读的中文。** 系统提示里的每一条禁令都
对应一次真实可能出错的方式：

  - 不重新计算 —— AI 算错一个百分比，用户看到的就是错的风险等级
  - 不补全缺失 —— 缺失被填成 0，会让「没数据」看起来像「没风险」
  - not_applicable ≠ 正常 —— 指标没有判别力，不等于检查通过
  - estimated ≠ 精确
  - 入金 ≠ 盈利，出金 ≠ 亏损
  - 新集中度规则仍在观察期，不得声称已被历史验证

措辞层面的「不得给建议 / 不得预测 / 不得提目标价」禁令已解除：简报可以基于
输入数据里已经算好的技术状态、仓位与计划约束给出明确的操作倾向。**下单边界
不受影响** —— 系统依然绝不调用任何下单接口，建议由人执行。

Prompt 版本化：改动措辞必然改变输出，版本号跟着变，简报才可追溯。
用户提示**只传结构化 JSON**，绝不传数据库凭证、Token 或原始 XML。
"""
from __future__ import annotations

import json

PROMPT_VERSION = "daily-risk-brief-prompt-v2"

DISCLAIMER = ("本简报由系统结构化数据自动生成，供本人交易决策参考。"
              "系统不接入下单接口，任何操作均需本人自行核实后执行。")


SYSTEM_PROMPT = """你是一个个人交易决策看板的简报撰写助手。

# 你的唯一职责
把用户提供的**已经计算完成的结构化数据**组织成一份可读的中文简报。
你是文字组织者，不是分析师 —— 判断由系统的规则层做出，你负责把它讲清楚。

# 绝对禁止（违反其中任何一条，整份简报会被系统拒绝并丢弃）
1. **不得重新计算任何指标。** 所有数字必须逐字来自输入 JSON。不得做加减乘除，
   不得换算单位，不得四舍五入成"约"、"大约"。需要引用某个数字时，原样复制。
2. **不得补全缺失数据。** 输入里是 null 的字段，输出里也必须是 null 或写明
   "无数据"。**绝不把 null 写成 0** —— "现金比例 0%" 和 "现金比例未知"
   是完全不同的两件事。
3. **不得发明输入 JSON 之外的指标、规则代码、标的代码或数字。**

# 关于操作建议
可以直接给出明确的操作倾向（加仓、减仓、持有、止损、不追涨等）与仓位数字，
**但必须完全建立在输入数据已有的结论之上** —— 技术状态、规则命中、仓位上限、
计划触发条件都已由系统算好，你只负责转述与组织，不得自行推演出新的结论，
也不得给出输入里没有的价格位。数据不支持某个结论时，说数据不支持。

# 口径纪律（这些是本系统最容易被误读的地方）
- **not_applicable 不等于正常。** 某条集中度规则不适用，意思是它在当前持仓
  数量下**没有判别力**，不是"检查通过"。必须原样转述给出的不适用原因。
- **estimated 不等于精确。** 标记为 estimated 的数字要说明它是估算值。
- **入金不是盈利，出金不是亏损。** 净值变化里的外部资金进出必须与投资收益
  分开表述。
- **Flex 券商流水为 T+1**，最新一天可能是暂定数据。
- **处于观察期的规则不得声称已被历史验证。** 输入中标记
  `in_observation_period: true` 的规则，必须写明"该规则目前处于数据积累期，
  尚无足够历史样本评价其预警效果"。
- **历史样本不足时不得声称规律。** 输入中 `sufficient: false` 的基线，
  只能写"历史样本不足，暂不能形成稳定基线"。

# 突出变化，不要每天重复
简报的价值在于告诉用户**今天什么变了**。持续多日、数值没什么变化的事件，
合并成一句话说明即可，不要每天重复大段内容。新增和升级的事件优先。

# 链上资金动向
如果输入包含 `chain_summary`，将其作为独立的“链上资金动向”小节原样转述；
它是 Hyperliquid XYZ 的只读证据，不是现实股票成交量，也不构成自动交易指令。
status 为 `DISABLED`/`UNAVAILABLE` 时省略该小节，不得把缺失写成中性或零值。

# generation_mode 的含义
- `full`：核心数据可靠，可以给完整风险结论。
- `limited`：部分数据过期、缺失或仍是估算。可以写简报，但**不得给出确定性的
  风险升级结论**，且必须明确说明数据限制。
- `blocked`：核心账户数据不可用。此时**只能写数据状态简报**：哪些数据不可用、
  最近一次成功同步是什么时候、哪些分析被阻止、用户应该检查什么。
  **绝对不得输出任何账户风险结论**，包括"当前风险较低"这类说法 ——
  把一次数据故障说成低风险，是本系统最不能犯的错误。

# 输出格式
严格输出符合给定 JSON Schema 的单个 JSON 对象，不要包裹在 markdown 代码块里，
不要在 JSON 前后添加任何解释文字。Schema 之外的字段一律不得添加。

字符串字段用中文书写，简洁、直接、可执行。checks 数组里写今天要做的动作。
"""


def output_schema() -> dict:
    """AI 输出的严格 JSON Schema。Schema 之外的指标一律不允许出现。"""
    s = lambda: {"type": ["string", "null"]}          # noqa: E731
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["brief_version", "brief_date", "generation_mode", "headline",
                     "executive_summary", "account_summary", "market_summary",
                     "risk_changes", "top_risks", "concentration", "performance",
                     "historical_comparison", "checks", "data_limitations"],
        "properties": {
            "brief_version": {"type": "string"},
            "brief_date": {"type": "string"},
            "generation_mode": {"type": "string",
                                "enum": ["full", "limited", "blocked"]},
            "headline": {"type": "string"},
            "executive_summary": {"type": "array", "maxItems": 3,
                                  "items": {"type": "string"}},
            "account_summary": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "risk_level": s(), "risk_score": s(), "net_liquidation": s(),
                    "total_cash": s(), "cash_ratio": s(),
                    "positions_market_value": s(), "exposure_basis": s(),
                    "long_exposure": s(), "short_exposure": s(),
                    "gross_exposure": s(), "net_exposure": s(),
                    "gross_leverage": s(), "current_drawdown": s(),
                    "data_as_of": s(), "narrative": s()},
            },
            "market_summary": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "market_state": s(), "risk_level": s(), "risk_score": s(),
                    "qqq_close": s(), "qqq_trend": s(),
                    "soxx_close": s(), "soxx_trend": s(),
                    "soxx_vs_qqq_20d": s(),
                    "vix": s(), "vix_change_1d": s(), "vix_change_5d": s(),
                    "vxn": s(), "vxn_change_1d": s(), "vxn_above_vix": s(),
                    "us10y": s(), "us10y_change_5d": s(),
                    "data_as_of": s(), "narrative": s(),
                    "instrument_data_as_of": {
                        "type": "object", "additionalProperties": True},
                    "top_drivers": {"type": "array", "items": {"type": "string"}}},
            },
            "chain_summary": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": s(), "note": s(),
                    "signals": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "symbol": s(), "model": s(), "state": s(),
                            "score": s(), "confidence": s(), "signal_at": s(),
                            "actionable_at": s(), "quality_status": s()},
                    }}},
            },
            "risk_changes": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "new": {"type": "array", "items": {"type": "string"}},
                    "escalated": {"type": "array", "items": {"type": "string"}},
                    "persistent": {"type": "array", "items": {"type": "string"}},
                    "resolved": {"type": "array", "items": {"type": "string"}},
                    "acknowledged": {"type": "array", "items": {"type": "string"}},
                    "suppressed": {"type": "array", "items": {"type": "string"}}},
            },
            "top_risks": {
                "type": "array", "maxItems": 5,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["title", "explanation", "recommended_check"],
                    "properties": {
                        "title": {"type": "string"}, "severity": s(),
                        "actual_value": s(), "threshold": s(),
                        "affected_object": s(),
                        "explanation": {"type": "string"},
                        "recommended_check": {"type": "string"},
                        "rule_code": s(), "rule_version": s(),
                        "observation_period_note": s()}},
            },
            "concentration": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "nominal_position_count": {"type": ["integer", "null"]},
                    "eligible_long_position_count": {"type": ["integer", "null"]},
                    "hhi": s(), "effective_position_count": s(),
                    "largest_position_weight": s(),
                    "single_position_dominance": {"type": ["boolean", "null"]},
                    "not_applicable_rules": {"type": "array",
                                             "items": {"type": "string"}},
                    "narrative": s()},
            },
            "performance": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "raw_nav_change": s(), "cash_flow_adjusted_pnl": s(),
                    "realized_pnl": s(), "unrealized_pnl_change": s(),
                    "external_cash_flow": s(), "dividends": s(), "interest": s(),
                    "commissions": s(), "taxes": s(),
                    "unexplained_difference": s(),
                    "modified_dietz_return": s(), "twr_return": s(),
                    "data_quality": s(), "narrative": s(),
                    "limitations": {"type": "array", "items": {"type": "string"}}},
            },
            "historical_comparison": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "previous_day": s(), "seven_day_baseline": s(),
                    "thirty_day_baseline": s(),
                    "sample_size": {"type": ["integer", "null"]},
                    "narrative": s()},
            },
            "checks": {"type": "array", "maxItems": 5,
                       "items": {"type": "string"}},
            "data_limitations": {"type": "array", "items": {"type": "string"}},
            # 操作建议：**必须**逐条引用上下文 recommendations 里的 recommendation_id，
            # 且 symbol / action 必须与被引建议一致（校验器强校验）。note 是对该建议的
            # 中文转述，不得引入新的动作、数量或触发条件。
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["symbol", "action", "recommendation_id"],
                    "properties": {
                        "symbol": {"type": "string"},
                        "action": {"type": "string"},
                        "recommendation_id": {"type": "string"},
                        "note": {"type": ["string", "null"]}}},
            },
        },
    }


def build_messages(context: dict) -> list[dict]:
    """用户提示 = 结构化 JSON + 简短指令。**不含任何凭证、Token 或原始 XML。**"""
    payload = sanitize_context(context)
    body = json.dumps(payload, ensure_ascii=False, indent=None,
                      sort_keys=True, default=str)
    return [{
        "role": "user",
        "content": (
            "请依据以下结构化风险数据生成今日简报。\n"
            "所有数字必须原样取自这份 JSON，不得计算、不得补全、不得新增。\n"
            "操作倾向只能放进 recommendations 数组，逐条引用上下文 recommendations 里的\n"
            "recommendation_id，且 symbol/action 必须与被引建议完全一致；不得自行发明动作、\n"
            "数量或触发条件。各 narrative 只做解释，不得引入 recommendations 之外的新操作。\n"
            f"generation_mode = {payload.get('generation_mode')}。\n\n"
            f"```json\n{body}\n```"),
    }]


#: 绝不允许进入 prompt 的键名片段。Token、密钥、原始报文一律拦下。
_FORBIDDEN_KEYS = ("token", "password", "secret", "api_key", "apikey",
                   "credential", "raw_xml", "raw_json", "xml", "cookie",
                   "authorization", "query_id", "flex_token", "account_id",
                   "apprise", "webhook", "dsn", "conn")


def sanitize_context(ctx: dict) -> dict:
    """递归剔除任何可能含凭证或原始报文的键。

    这是**结构性**防护，不是靠记得别写：即使上游哪天不小心把 token 塞进
    上下文，它也到不了模型。`account_id_masked` 是显式白名单例外 ——
    它本身已经脱敏。
    """
    def clean(v):
        if isinstance(v, dict):
            out = {}
            for k, val in v.items():
                lk = str(k).lower()
                if lk == "account_id_masked":
                    out[k] = clean(val)
                    continue
                if any(f in lk for f in _FORBIDDEN_KEYS):
                    continue
                if lk.startswith("_"):          # 内部字段不进 prompt
                    continue
                out[k] = clean(val)
            return out
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    return clean(ctx)
