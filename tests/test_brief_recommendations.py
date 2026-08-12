"""Phase 3：简报操作建议 provenance 校验。

措辞松绑后的机器护栏——简报里的操作倾向必须引用真实存在的 recommendation_id，
且不得改写确定性合成建议的 action。不做关键词黑名单。
"""
from app import brief_validator as bv
from app import brief_prompt as bp


_CTX = {"recommendations": [
    {"recommendation_id": "rec_1", "symbol": "RKLB", "action": "DO_NOT_ADD"},
    {"recommendation_id": "rec_2", "symbol": "MRVL", "action": "REDUCE"},
]}


def test_valid_reference_passes():
    out = {"recommendations": [
        {"recommendation_id": "rec_1", "symbol": "RKLB",
         "action": "DO_NOT_ADD", "note": "仓位已超上限"}]}
    errs, _ = bv._check_recommendations(out, _CTX)
    assert errs == []


def test_nonexistent_recommendation_id_blocked():
    out = {"recommendations": [
        {"recommendation_id": "rec_ZZZ", "symbol": "RKLB", "action": "DO_NOT_ADD"}]}
    errs, _ = bv._check_recommendations(out, _CTX)
    assert errs and "不存在的 recommendation_id" in errs[0]


def test_tampered_action_blocked():
    """LLM 不能把确定性建议的动作改成别的（DO_NOT_ADD → ADD_WATCH）。"""
    out = {"recommendations": [
        {"recommendation_id": "rec_1", "symbol": "RKLB", "action": "ADD_WATCH"}]}
    errs, _ = bv._check_recommendations(out, _CTX)
    assert errs and "篡改" in errs[0]


def test_mismatched_symbol_blocked():
    out = {"recommendations": [
        {"recommendation_id": "rec_1", "symbol": "NVDA", "action": "DO_NOT_ADD"}]}
    errs, _ = bv._check_recommendations(out, _CTX)
    assert errs and "不一致" in errs[0]


def test_missing_id_blocked():
    out = {"recommendations": [{"symbol": "RKLB", "action": "HOLD"}]}
    errs, _ = bv._check_recommendations(out, _CTX)
    assert errs and "缺少 recommendation_id" in errs[0]


def test_no_recommendations_is_fine():
    assert bv._check_recommendations({}, {}) == ([], [])


def test_validate_wires_the_check():
    """recommendations_grounded 已接入 validate 主流程。"""
    content = _min_brief()
    content["recommendations"] = [
        {"recommendation_id": "rec_ZZZ", "symbol": "RKLB", "action": "DO_NOT_ADD"}]
    r = bv.validate(content, {**_min_ctx(), **_CTX})
    assert any("不存在的 recommendation_id" in e for e in r["errors"]), r["errors"]


def test_output_schema_allows_recommendations():
    schema = bp.output_schema()
    rec = schema["properties"]["recommendations"]
    assert rec["type"] == "array"
    assert set(rec["items"]["required"]) == {"symbol", "action", "recommendation_id"}


def _min_brief() -> dict:
    return {"brief_version": "v1", "brief_date": "2026-08-08",
            "generation_mode": "full", "headline": "x",
            "executive_summary": [], "account_summary": {}, "market_summary": {},
            "risk_changes": {}, "top_risks": [], "concentration": {},
            "performance": {}, "historical_comparison": {}, "checks": [],
            "data_limitations": []}


def _min_ctx() -> dict:
    return {"generation_mode": "full", "account": {}, "market": {},
            "top_risks": [], "risk_changes": {}}
