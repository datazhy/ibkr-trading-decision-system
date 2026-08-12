"""简报输出校验器。**这是简报能不能发出去的最后一道闸门。**

校验不通过 = 不发送、不推送、记 validation_failed。宁可当天只有模板简报，
也不能把一份编造了数字的简报送到用户面前。

校验的是**数据可追溯性**，不再限制措辞 —— 简报可以给出明确的操作倾向，
只要每个数字都能在输入上下文里找到出处。

十项检查（对应验收清单）：
  1. JSON Schema 结构
  2. 所有金额与比例都能在输入上下文里找到
  3. 不引用不存在的 symbol
  4. 不引用不存在的规则
  5. null 没有被写成 0
  6. blocked 模式没有输出账户风险结论
  7. not_applicable 没有被写成正常
  8. 数据时间与上下文一致
  9. 没有遗漏 critical/high 新事件
 10. 没有泄露完整账户标识
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from . import brief_context as bc
from . import brief_prompt as bp

_d = bc._d

#: 数字提取：匹配金额/比例/评分。带千分位与负号。
_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")

#: Decimal 可解析但不能量化的非有限数字。上下文出现时忽略，输出出现时明确拒绝。
_NONFINITE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:s?nan|inf(?:inity)?)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

#: 日期与时间戳形态。**必须先剔除**，否则 "2026-07-29" 里的 2026 会被当成
#: 编造的指标值 —— 那是校验器自己制造的假阳性，会淹没真正的问题。
_DATETIME_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)?"
    r"|\d{4}-\d{2}(?![-\d])"          # YYYY-MM（如「2026-07 上线」）
    r"|\d{2}:\d{2}(?::\d{2})?"
    # Flex 的 YYYYMMDD。**必须加边界** —— 无边界的 \d{8} 会在长小数
    # （HHI = 0.5102040816…）中间挖掉一段，剩下的碎片被当成编造的数字。
    r"|(?<![\d.])\d{8}(?![\d.])")

#: **标识符字段不参与数字追溯。** 版本串、哈希、ID、规则代码里的数字是
#: 标识的一部分，不是指标值 —— 生产实测 rule_version
#: "risk-v2-conc/2026-07~797fe2b69126ff3b" 里的哈希片段被判成编造的数字，
#: 导致整份合法简报被拒发。
_IDENTIFIER_SUFFIXES = ("_version", "_hash", "_id", "rule_code", "_code",
                        "field_path", "source_table", "context_hash",
                        "brief_version", "prompt_version")


def _is_identifier_path(path: str) -> bool:
    leaf = path.rsplit(".", 1)[-1].split("[")[0]
    return any(leaf.endswith(sfx) or leaf == sfx.lstrip("_")
               for sfx in _IDENTIFIER_SUFFIXES)


#: 允许在文字里自由出现的小数字 —— 它们是计数（"3 起新增"）而非指标值，
#: 强行要求可追溯只会产生大量假阳性。
_FREE_NUMBER_MAX = Decimal("1000")

#: 完整账户号形态（IBKR 为 U + 8 位数字）
_ACCOUNT_RE = re.compile(r"\bU\d{7,9}\b")

#: blocked 模式下禁止出现的风险结论措辞
_BLOCKED_CONCLUSION = ("风险较低", "风险偏低", "当前风险低", "风险可控",
                       "账户风险为 low", "风险等级为低", "总体安全", "无风险",
                       "风险不高", "状况良好")

#: 把 not_applicable 说成"正常/通过"的措辞
_NA_AS_NORMAL = ("规则通过", "检查通过", "指标正常", "均正常", "全部正常",
                 "未触发，正常", "集中度正常", "无集中风险", "通过检查")

#: 否定前缀。「不适用**不等于规则通过**」是本系统要求的正确表述，
#: 它含有「规则通过」这个子串 —— 不排除否定式，正确文案会被自己的校验判违规。
_NEGATIONS = ("不等于", "不是", "并非", "不代表", "≠", "不意味", "不能理解为",
              "不应视为", "不视为")


def validate(content: dict, context: dict) -> dict:
    """返回 {valid, errors, warnings, checks}。"""
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict] = []

    def run(name: str, fn):
        try:
            errs, warns = fn()
        except Exception as exc:                          # noqa: BLE001
            errs, warns = [f"{name} 校验自身出错：{exc}"], []
        checks.append({"check": name, "passed": not errs,
                       "errors": errs, "warnings": warns})
        errors.extend(errs)
        warnings.extend(warns)

    run("schema", lambda: _check_schema(content))
    run("numbers_traceable", lambda: _check_numbers(content, context))
    run("symbols_exist", lambda: _check_symbols(content, context))
    run("rules_exist", lambda: _check_rules(content, context))
    run("null_not_zero", lambda: _check_null_not_zero(content, context))
    run("recommendations_grounded", lambda: _check_recommendations(content, context))
    run("blocked_no_conclusion", lambda: _check_blocked(content, context))
    run("not_applicable_not_normal", lambda: _check_not_applicable(content, context))
    run("timestamps_match", lambda: _check_timestamps(content, context))
    run("no_missing_critical", lambda: _check_missing_events(content, context))
    run("no_account_leak", lambda: _check_account_leak(content, context))

    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "checks": checks}


# ---------------- 1. Schema ----------------

def _check_schema(content: dict) -> tuple[list[str], list[str]]:
    schema = bp.output_schema()
    errs = _validate_node(content, schema, "$")
    return errs, []


def _validate_node(node, schema: dict, path: str) -> list[str]:
    """极简 JSON Schema 校验。

    刻意不引第三方依赖 —— 需要的子集很小（type/required/enum/
    additionalProperties/maxItems/items/properties），自己实现比多一个
    依赖更可控，也让校验规则本身可读。
    """
    errs: list[str] = []
    types = schema.get("type")
    types = [types] if isinstance(types, str) else (types or [])

    def type_ok(v, t: str) -> bool:
        return {
            "object": isinstance(v, dict), "array": isinstance(v, list),
            "string": isinstance(v, str), "integer": isinstance(v, bool) is False
            and isinstance(v, int), "number": isinstance(v, (int, float))
            and not isinstance(v, bool),
            "boolean": isinstance(v, bool), "null": v is None,
        }.get(t, True)

    if types and not any(type_ok(node, t) for t in types):
        return [f"{path}: 类型应为 {types}，实际 {type(node).__name__}"]

    if "enum" in schema and node not in schema["enum"]:
        errs.append(f"{path}: 取值 {node!r} 不在允许集合 {schema['enum']}")

    if isinstance(node, dict) and "properties" in schema:
        for req in schema.get("required", []):
            if req not in node:
                errs.append(f"{path}: 缺少必填字段 {req}")
        if schema.get("additionalProperties") is False:
            extra = set(node) - set(schema["properties"])
            for k in sorted(extra):
                errs.append(f"{path}: 出现 Schema 之外的字段 {k}")
        for k, sub in schema["properties"].items():
            if k in node:
                errs.extend(_validate_node(node[k], sub, f"{path}.{k}"))

    if isinstance(node, list):
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            errs.append(f"{path}: 元素数 {len(node)} 超过上限 {schema['maxItems']}")
        if "items" in schema:
            for i, item in enumerate(node):
                errs.extend(_validate_node(item, schema["items"], f"{path}[{i}]"))
    return errs


# ---------------- 2. 数字可追溯 ----------------

def _collect_numbers(obj) -> set[str]:
    """上下文里出现过的所有数字的规范形式（含合法展示形式）。"""
    out: set[str] = set()

    def register(d: Decimal) -> None:
        # 精确规范形式（4 位）——原始对账口径。
        exact = _canon(d)
        if exact is not None:
            out.add(exact)
        # 合法展示形式：展示层把长小数统一收敛到 2 位（brief_generator._DISPLAY_Q）。
        # 不登记就会把 1001.088334 → 1001.09 这种正常舍入判成「编造」，
        # 从而阻断整份简报的投递（曾致生产简报连续多日 validation failed）。
        # 只登记「输入数字能确定性推导出的」两位形式，仍是精确集合、不引入模糊容差。
        displayed = _canon(d, Decimal("0.01"))
        if displayed is not None:
            out.add(displayed)

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif v is not None and not isinstance(v, bool):
            d = _d(v)
            if d is not None:
                register(d)
                # 比例同时登记百分数形式：上下文存 0.3144，简报写 31.44%
                register(d * 100)
                register(d / 100)

    walk(obj)
    return out


def _canon(d: Decimal, quantum: Decimal = Decimal("0.0001")) -> str | None:
    """量化并去掉尾零；非有限或超出 Decimal 精度的值返回 None。"""
    if not d.is_finite():
        return None
    try:
        q = d.quantize(quantum)
    except InvalidOperation:
        return None
    return format(q.normalize(), "f")


def _text_numbers(obj) -> list[tuple[str, str]]:
    """输出里出现的所有数字 + 所在字段路径。"""
    found: list[tuple[str, str]] = []

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str):
            # 先把日期/时间戳挖掉，再找数字
            stripped = _DATETIME_RE.sub(" ", v)
            for m in _NUM_RE.finditer(stripped):
                found.append((m.group(0).replace(",", ""), path))
        elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            found.append((str(v), path))

    walk(obj, "$")
    return found


def _check_numbers(content: dict, context: dict) -> tuple[list[str], list[str]]:
    """输出里的每个「大数字」都必须能在上下文里找到出处。

    小数字（计数、年份、条数）放行 —— 强行追溯会淹没在假阳性里，
    而真正危险的是被编造出来的金额和比例。
    """
    allowed = _collect_numbers(context)
    errs, warns = [], []

    def reject_nonfinite(v, path="$"):
        if isinstance(v, dict):
            for k, x in v.items():
                reject_nonfinite(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                reject_nonfinite(x, f"{path}[{i}]")
        elif isinstance(v, str):
            for m in _NONFINITE_RE.finditer(v):
                errs.append(f"{path}: 出现非法非有限数字 {m.group(0)}")
        elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
            try:
                d = Decimal(str(v))
            except (InvalidOperation, TypeError, ValueError):
                return
            if not d.is_finite():
                errs.append(f"{path}: 出现非法非有限数字 {v}")

    reject_nonfinite(content)
    for raw, path in _text_numbers(content):
        if _is_identifier_path(path):
            continue
        d = _d(raw)
        if d is None or abs(d) <= _FREE_NUMBER_MAX:
            continue
        exact = _canon(d)
        displayed = _canon(d, Decimal("0.01"))
        if exact is None or displayed is None:
            errs.append(f"{path}: 数字 {raw} 无法安全规范化")
            continue
        if exact in allowed or displayed in allowed:
            continue
        errs.append(f"{path}: 数字 {raw} 在输入上下文中不存在（疑似编造或重新计算）")
    return errs, warns


# ---------------- 3/4. symbol 与规则 ----------------

def _known_symbols(context: dict) -> set[str]:
    syms: set[str] = set()
    for p in (context.get("account") or {}).get("_positions") or []:
        if p.get("symbol"):
            syms.add(str(p["symbol"]))
    for it in (context.get("risk_changes") or {}).get("ordered") or []:
        if it.get("affected"):
            syms.add(str(it["affected"]))
    for t in context.get("top_risks") or []:
        if t.get("affected_object"):
            syms.add(str(t["affected_object"]))
    for x in (context.get("concentration") or {}).get("excluded_positions") or []:
        if x.get("symbol"):
            syms.add(str(x["symbol"]))
    syms.update({"QQQ", "SOXX", "VIX", "VXN", "TNX"})    # 市场指标固定集合
    return syms


def _check_symbols(content: dict, context: dict) -> tuple[list[str], list[str]]:
    known = _known_symbols(context)
    errs = []
    for t in content.get("top_risks") or []:
        a = t.get("affected_object")
        if a and str(a) not in known:
            errs.append(f"top_risks: 引用了上下文中不存在的标的 {a}")
    return errs, []


def _known_rules(context: dict) -> set[str]:
    rules: set[str] = set()
    for it in (context.get("risk_changes") or {}).get("ordered") or []:
        if it.get("rule_code"):
            rules.add(str(it["rule_code"]))
    rules.update(str(r) for r in
                 ((context.get("account") or {}).get("triggered_rules") or []))
    rules.update(str(r) for r in
                 ((context.get("market") or {}).get("triggered_rules") or []))
    for x in (context.get("concentration") or {}).get("not_applicable_rules") or []:
        if x.get("rule_code"):
            rules.add(str(x["rule_code"]))
    return rules


_RULE_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,5}\b")


def _check_rules(content: dict, context: dict) -> tuple[list[str], list[str]]:
    known = _known_rules(context)
    errs = []
    for t in content.get("top_risks") or []:
        rc = t.get("rule_code")
        if rc and str(rc) not in known:
            errs.append(f"top_risks: 引用了上下文中不存在的规则 {rc}")
    # 正文里若出现规则代码样式的 token，同样要求存在
    for raw, path in _tokens(content, _RULE_TOKEN):
        if raw not in known and not raw.startswith(("HHI", "TWR", "NAV", "VIX",
                                                    "VXN", "QQQ", "SOXX")):
            errs.append(f"{path}: 提到未知规则代码 {raw}")
    return errs, []


def _check_recommendations(content: dict, context: dict) -> tuple[list[str], list[str]]:
    """操作建议 provenance：简报里每条 recommendation 必须引用上下文里真实存在的
    recommendation_id，且 symbol / action 与被引建议完全一致。

    这是措辞松绑后的机器护栏：LLM 不能凭空推演无证据的操作，也不能改写确定性
    合成建议的动作。不做关键词黑名单。
    """
    ctx_recos = {r.get("recommendation_id"): r
                 for r in (context.get("recommendations") or [])
                 if r.get("recommendation_id")}
    errs: list[str] = []
    for i, rec in enumerate(content.get("recommendations") or []):
        path = f"recommendations[{i}]"
        rid = rec.get("recommendation_id")
        if not rid:
            errs.append(f"{path}: 操作建议缺少 recommendation_id，无法追溯")
            continue
        src = ctx_recos.get(rid)
        if src is None:
            errs.append(f"{path}: 引用了不存在的 recommendation_id「{rid}」")
            continue
        if rec.get("symbol") and rec.get("symbol") != src.get("symbol"):
            errs.append(f"{path}: symbol「{rec.get('symbol')}」与建议 {rid} "
                        f"的「{src.get('symbol')}」不一致")
        if rec.get("action") and rec.get("action") != src.get("action"):
            errs.append(f"{path}: action「{rec.get('action')}」篡改了建议 {rid} "
                        f"的确定性动作「{src.get('action')}」")
    return errs, []


def _tokens(obj, pattern: re.Pattern) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str):
            for m in pattern.finditer(v):
                out.append((m.group(0), path))

    walk(obj, "$")
    return out


# ---------------- 5. null 不能写成 0 ----------------

_NULL_FIELDS = (
    ("account_summary", "net_liquidation"), ("account_summary", "cash_ratio"),
    ("account_summary", "gross_leverage"), ("account_summary", "current_drawdown"),
    ("account_summary", "risk_score"), ("account_summary", "total_cash"),
    ("account_summary", "positions_market_value"),
    ("account_summary", "long_exposure"), ("account_summary", "gross_exposure"),
    ("performance", "cash_flow_adjusted_pnl"), ("performance", "realized_pnl"),
    ("performance", "unrealized_pnl_change"),
    ("performance", "modified_dietz_return"), ("performance", "twr_return"),
    ("concentration", "hhi"), ("concentration", "effective_position_count"),
    ("concentration", "largest_position_weight"),
)

_CTX_PATH = {
    ("account_summary", "net_liquidation"): ("account", "net_liquidation"),
    ("account_summary", "cash_ratio"): ("account", "cash_ratio"),
    ("account_summary", "gross_leverage"): ("account", "gross_leverage"),
    ("account_summary", "current_drawdown"): ("account", "current_drawdown"),
    ("account_summary", "risk_score"): ("account", "risk_score"),
    ("account_summary", "total_cash"): ("account", "total_cash"),
    ("account_summary", "positions_market_value"): ("account", "positions_market_value"),
    ("account_summary", "long_exposure"): ("account", "long_exposure"),
    ("account_summary", "gross_exposure"): ("account", "gross_exposure"),
    ("performance", "cash_flow_adjusted_pnl"): ("performance", "cash_flow_adjusted_pnl"),
    ("performance", "realized_pnl"): ("performance", "realized_pnl"),
    ("performance", "unrealized_pnl_change"): ("performance", "unrealized_pnl_change"),
    ("performance", "modified_dietz_return"): ("performance", "modified_dietz_return"),
    ("performance", "twr_return"): ("performance", "twr_return"),
    ("concentration", "hhi"): ("concentration", "hhi"),
    ("concentration", "effective_position_count"): ("concentration",
                                                    "effective_position_count"),
    ("concentration", "largest_position_weight"): ("concentration",
                                                   "largest_position_weight"),
}


def _check_null_not_zero(content: dict, context: dict) -> tuple[list[str], list[str]]:
    """上下文是 null 的字段，输出里绝不能变成 0。

    这是本系统最不能容忍的错误之一：把「没数据」显示成「0」，
    会让一个数据故障看起来像一个安全的账户。
    """
    errs = []
    for sec, field in _NULL_FIELDS:
        cs, cf = _CTX_PATH[(sec, field)]
        ctx_val = (context.get(cs) or {}).get(cf)
        if ctx_val not in (None, ""):
            continue
        out_val = (content.get(sec) or {}).get(field)
        if out_val in (None, ""):
            continue
        d = _d(out_val)
        if d is not None and d == 0:
            errs.append(f"{sec}.{field}: 上下文为 null，输出却写成 0 —— "
                        "缺失数据不得显示为零")
        elif d is not None:
            errs.append(f"{sec}.{field}: 上下文为 null，输出却给出数值 {out_val}")
    return errs, []


# ---------------- 6/7. 交易指令与预测 ----------------

def _all_text(obj) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def walk(v, path):
        if isinstance(v, dict):
            for k, x in v.items():
                walk(x, f"{path}.{k}")
        elif isinstance(v, list):
            for i, x in enumerate(v):
                walk(x, f"{path}[{i}]")
        elif isinstance(v, str):
            out.append((v, path))

    walk(obj, "$")
    return out


# ---------------- 6. blocked 模式 ----------------

def _check_blocked(content: dict, context: dict) -> tuple[list[str], list[str]]:
    if context.get("generation_mode") != bc.GATE_BLOCKED:
        return [], []
    errs = []
    if content.get("generation_mode") != bc.GATE_BLOCKED:
        errs.append("blocked 模式下 generation_mode 必须为 blocked")
    a = content.get("account_summary") or {}
    if a.get("risk_level") not in (None, ""):
        errs.append("blocked 模式下不得输出账户风险等级")
    if a.get("risk_score") not in (None, ""):
        errs.append("blocked 模式下不得输出账户风险评分")
    if content.get("top_risks"):
        errs.append("blocked 模式下不得输出最大风险来源")
    for text, path in _all_text(content):
        for phrase in _BLOCKED_CONCLUSION:
            if phrase in text:
                errs.append(f"{path}: blocked 模式下出现风险结论措辞「{phrase}」——"
                            "把数据故障说成低风险是最危险的错误")
    return errs, []


# ---------------- 9. not_applicable ----------------

def _check_not_applicable(content: dict, context: dict) -> tuple[list[str], list[str]]:
    # blocked 简报刻意不输出任何集中度分析，因此「有没有正确呈现不适用」
    # 在这个模式下不适用（同 11 项的关键事件检查）
    if context.get("generation_mode") == bc.GATE_BLOCKED:
        return [], []
    na = (context.get("concentration") or {}).get("not_applicable_rules") or []
    if not na:
        return [], []
    errs = []
    conc = content.get("concentration") or {}
    out_na = conc.get("not_applicable_rules") or []
    if not out_na:
        errs.append("concentration: 上下文有不适用规则，输出却未列出 —— "
                    "not_applicable 必须显式呈现，不适用不等于正常")
    text = " ".join(t for t, _ in _all_text(conc))
    for phrase in _NA_AS_NORMAL:
        if _asserted(text, phrase):
            errs.append(f"concentration: 把不适用规则表述成「{phrase}」—— "
                        "不适用不等于规则通过")
    return errs, []


# ---------------- 10. 时间一致 ----------------

def _asserted(text: str, phrase: str) -> bool:
    """phrase 是否被**肯定地**断言（而非出现在否定句里）。"""
    idx = 0
    while True:
        i = text.find(phrase, idx)
        if i < 0:
            return False
        prefix = text[max(0, i - 12):i]
        if not any(n in prefix for n in _NEGATIONS):
            return True
        idx = i + len(phrase)


def _check_timestamps(content: dict, context: dict) -> tuple[list[str], list[str]]:
    errs, warns = [], []
    pairs = (
        (("account_summary", "data_as_of"), ("account", "data_as_of")),
        (("market_summary", "data_as_of"), ("market", "data_as_of")),
    )
    for (osec, of), (csec, cf) in pairs:
        out_v = (content.get(osec) or {}).get(of)
        ctx_v = (context.get(csec) or {}).get(cf)
        if out_v in (None, "") or ctx_v in (None, ""):
            continue
        if str(out_v) != str(ctx_v):
            errs.append(f"{osec}.{of}: 数据时间 {out_v} 与上下文 {ctx_v} 不一致")
    if content.get("brief_date") != context.get("brief_date"):
        errs.append("brief_date 与上下文不一致")
    return errs, warns


# ---------------- 11. 遗漏关键事件 ----------------

def _check_missing_events(content: dict, context: dict) -> tuple[list[str], list[str]]:
    """新增或升级的 critical / high 事件必须出现在简报里。

    漏报一条 critical 比多写一段废话严重得多。
    """
    if context.get("generation_mode") == bc.GATE_BLOCKED:
        return [], []
    changes = context.get("risk_changes") or {}
    must = [it for it in (changes.get("new") or []) + (changes.get("escalated") or [])
            if str(it.get("severity") or "").lower() in ("high", "critical")]
    if not must:
        return [], []
    blob = " ".join(t for t, _ in _all_text(content))
    errs = []
    for it in must:
        code = str(it.get("rule_code") or "")
        title = str(it.get("title") or "")
        if code and code in blob:
            continue
        if title and title in blob:
            continue
        errs.append(f"遗漏了新增/升级的 {it.get('severity')} 事件：{code or title}")
    return errs, []


# ---------------- 12. 账户标识泄露 ----------------

def _check_account_leak(content: dict, context: dict) -> tuple[list[str], list[str]]:
    errs = []
    for text, path in _all_text(content):
        m = _ACCOUNT_RE.search(text)
        if m:
            errs.append(f"{path}: 泄露了完整账户标识 {m.group(0)[:2]}***")
    return errs, []
