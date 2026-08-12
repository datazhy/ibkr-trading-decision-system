"""FastAPI 看板（Phase 3）。只读三页 + CSV 导出。

安全：本进程只监听 127.0.0.1，由 Caddy 反代 + BasicAuth 对外暴露（见 deploy/Caddyfile）。
不含任何下单/改单端点。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import (webauth, webbriefs, webdata, webevents, webflex, webreplay,
               webresearch, webtechnical)
from . import position_plans as position_plan_service
from . import opportunity_service as opportunity_service
from . import webhyperliquid
from . import x_service
from .api import build_router
from .config import Config, load_config
from .db import Database
from .position_plan_view import (
    action_label as pp_action_label,
    action_status_label as pp_action_status_label,
    event_type_label as pp_event_type_label,
    format_datetime as pp_format_datetime,
    format_input_datetime as pp_format_input_datetime,
    format_money as pp_format_money,
    format_percent as pp_format_percent,
    format_price as pp_format_price,
    format_quantity as pp_format_quantity,
    match_label as pp_match_label,
    relative_time as pp_relative_time,
    status_label as pp_status_label,
    trigger_label as pp_trigger_label,
)

_TEMPLATES = Path(__file__).parent / "templates"


def _bounds(start: str | None, end: str | None, tz):
    """把 ET 日期（YYYY-MM-DD）转成 UTC ISO 上下界。"""
    start_iso = end_iso = None
    if start:
        try:
            d = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=tz)
            start_iso = d.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    if end:
        try:
            d = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).replace(tzinfo=tz)
            end_iso = d.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    return start_iso, end_iso


def _market_ctx(cfg, db, now):
    """所有页面共享的市场时段徽标（导航栏用）。"""
    st = webdata.data_status_bar(cfg, db, now)
    return {"market_note": st.get("market_note"), "health_css": st["overall_css"]}


def create_app(cfg: Config, db: Database, sync_provider=None,
               notifier=None) -> FastAPI:
    app = FastAPI(title="小鱼交易台", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATES))
    templates.env.globals.update(
        money=webdata.money, pct=webdata.pct, signed=webdata.signed_money,
        num=webdata.num, clean_decimals=webdata.clean_decimals,
        abs_time=lambda ts: webdata.abs_time(ts, cfg.market_tz),
        rel=lambda ts: webdata.rel_time(ts, datetime.now(timezone.utc)),
        pp_status=pp_status_label,
        pp_action=pp_action_label,
        pp_action_status=pp_action_status_label,
        pp_event=pp_event_type_label,
        pp_trigger=pp_trigger_label,
        pp_match=pp_match_label,
        pp_price=pp_format_price,
        pp_money=pp_format_money,
        pp_qty=pp_format_quantity,
        pp_pct=pp_format_percent,
        pp_time=lambda ts: pp_format_datetime(ts, cfg.local_tz),
        pp_input_time=lambda ts: pp_format_input_datetime(ts, cfg.local_tz),
        pp_relative=lambda ts: pp_relative_time(
            ts, datetime.now(timezone.utc), cfg.local_tz),
    )

    # ---------------- 登录保护 ----------------
    # 整站中间件：除放行清单外，一切请求都需有效会话 Cookie。
    # 页面请求未登录 → 302 跳登录页并带 next；API 请求 → 401 JSON（不重定向，
    # 免得脚本把登录页 HTML 当成数据）。
    _AUTH_ALLOW = {"/login", "/logout", "/healthz", "/favicon.ico"}

    def _safe_next(nxt: str | None) -> str:
        # 只接受站内绝对路径，挡开放重定向（//evil、http://… 一律丢弃）
        if nxt and nxt.startswith("/") and not nxt.startswith("//"):
            return nxt
        return "/"

    if cfg.dashboard_auth_enabled:
        @app.middleware("http")
        async def _require_login(request: Request, call_next):
            path = request.url.path
            if path in _AUTH_ALLOW:
                return await call_next(request)
            token = request.cookies.get(webauth.COOKIE_NAME)
            if webauth.verify_token(db, token):
                return await call_next(request)
            if path.startswith("/api/"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
            nxt = request.url.path
            if request.url.query:
                nxt += "?" + request.url.query
            return RedirectResponse("/login?" + urlencode({"next": nxt}),
                                    status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        # 已登录再访问登录页 → 直接回目标页，不用重复登录
        if cfg.dashboard_auth_enabled and webauth.verify_token(
                db, request.cookies.get(webauth.COOKIE_NAME)):
            return RedirectResponse(_safe_next(next), status_code=303)
        return templates.TemplateResponse(request, "login.html", {
            "next": _safe_next(next), "username": cfg.dashboard_auth_user})

    @app.post("/login", response_class=HTMLResponse)
    async def login_submit(request: Request):
        # 用 parse_qs 解析 urlencoded 表单，避免引入 python-multipart（全站约定，同 /journal）
        raw = (await request.body()).decode("utf-8")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        username = (form.get("username") or "").strip()
        password = form.get("password") or ""
        nxt = _safe_next(form.get("next"))
        if not webauth.has_password(db):
            return templates.TemplateResponse(request, "login.html", {
                "next": nxt, "username": username,
                "error": "尚未设置登录密码，请在服务器执行 manage.py webpass 设置。"},
                status_code=503)
        if not webauth.check_credentials(cfg, db, username, password):
            return templates.TemplateResponse(request, "login.html", {
                "next": nxt, "username": username,
                "error": "用户名或密码错误"}, status_code=401)
        token = webauth.issue_token(db, cfg.dashboard_auth_user,
                                    cfg.dashboard_session_ttl_hours)
        resp = RedirectResponse(nxt, status_code=303)
        resp.set_cookie(webauth.COOKIE_NAME, token, httponly=True, secure=True,
                        samesite="lax", max_age=cfg.dashboard_session_ttl_hours * 3600,
                        path="/")
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(webauth.COOKIE_NAME, path="/")
        return resp

    # JSON API（账户快照层）。web 进程不跑同步，故 sync_provider 为 None：
    # 手动触发会明确返回 503，而不是假装成功。
    app.include_router(build_router(cfg, db, sync_provider=sync_provider,
                                    notifier=notifier))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "readonly": True}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        now = datetime.now(timezone.utc)
        data = webdata.overview_data(cfg, db, now)
        ready_opportunities = []
        if cfg.opportunity_enabled:
            latest_opportunity_scan = opportunity_service.OpportunityService(cfg, db).latest_scan()
            if latest_opportunity_scan:
                ready_opportunities = [
                    c for c in latest_opportunity_scan["candidates"] if c["status"] == "READY"
                ][:3]
        return templates.TemplateResponse(request, "overview.html", {
            "page": "overview", "data": data,
            "ready_opportunities": ready_opportunities,
            "plan_error": _plan_error_ctx(request),
            "open_cycle_id": request.query_params.get("open"),
            "dstate": webdata.data_state_panel(cfg, db, now),
            "risk": webdata.risk_panel(cfg, db, now),
            "market": webdata.market_panel(cfg, db, now),
            **_market_ctx(cfg, db, now),
        })

    def _opportunity_redirect(message: str | None = None, error: str | None = None):
        query = urlencode({k: v for k, v in {"message": message, "error": error}.items() if v})
        return RedirectResponse("/opportunities" + ("?" + query if query else ""), status_code=303)

    @app.get("/opportunities", response_class=HTMLResponse)
    def opportunities_page(request: Request):
        now = datetime.now(timezone.utc)
        svc = opportunity_service.OpportunityService(cfg, db)
        shadow = svc.shadow_readiness()
        return templates.TemplateResponse(request, "opportunities.html", {
            "page": "opportunities", "enabled": cfg.opportunity_enabled,
            "builder_enabled": cfg.portfolio_builder_enabled,
            "shadow": shadow,
            "watchlist": svc.list_watchlist(), "scan": svc.latest_scan(),
            "proposal": svc.latest_proposal(),
            "outcome_summary": svc.outcome_summary(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            **_market_ctx(cfg, db, now),
        })

    @app.get("/hyperliquid", response_class=HTMLResponse)
    def hyperliquid_page(request: Request):
        return templates.TemplateResponse(request, "hyperliquid.html", {
            "page": "hyperliquid",
            "data": webhyperliquid.page_data(cfg),
        })

    @app.post("/opportunities/watchlist")
    async def opportunities_watchlist_save(request: Request):
        form = await _form(request)
        try:
            opportunity_service.OpportunityService(cfg, db).save_watch(
                form.get("symbol"), sector=(form.get("sector") or None),
                thesis=(form.get("thesis") or None), why_watch=(form.get("why_watch") or None),
                note=(form.get("note") or None), actor="dashboard")
        except opportunity_service.OpportunityError as exc:
            return _opportunity_redirect(error=str(exc))
        return _opportunity_redirect(message="已保存到个人自选池")

    @app.post("/opportunities/watchlist/{symbol}/status")
    async def opportunities_watchlist_status(request: Request, symbol: str):
        form = await _form(request)
        try:
            opportunity_service.OpportunityService(cfg, db).set_watch_status(
                symbol, form.get("status") or "PAUSED")
        except opportunity_service.OpportunityError as exc:
            return _opportunity_redirect(error=str(exc))
        return _opportunity_redirect(message="自选池状态已更新")

    @app.post("/opportunities/scan")
    def opportunities_scan():
        try:
            opportunity_service.OpportunityService(cfg, db).run_scan(trigger_source="manual")
        except opportunity_service.OpportunityError as exc:
            return _opportunity_redirect(error=str(exc))
        return _opportunity_redirect(message="机会扫描已完成")

    @app.post("/opportunities/portfolio")
    def opportunities_portfolio():
        try:
            opportunity_service.OpportunityService(cfg, db).build_portfolio()
        except opportunity_service.OpportunityError as exc:
            return _opportunity_redirect(error=str(exc))
        return _opportunity_redirect(message="组合建议已生成")

    @app.post("/opportunities/items/{item_id}/adopt")
    def opportunities_adopt(item_id: str):
        try:
            result = opportunity_service.OpportunityService(cfg, db).adopt_item(
                item_id, actor="dashboard")
        except (opportunity_service.OpportunityError,
                position_plan_service.PositionPlanError) as exc:
            return _opportunity_redirect(error=str(exc))
        return RedirectResponse(f"/position-plans/{result['cycle_id']}", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        now = datetime.now(timezone.utc)
        st = webdata.data_status_bar(cfg, db, now)
        return templates.TemplateResponse(request, "status.html", {
            "page": "status", "status": st, "market_note": st.get("market_note"),
            "health_css": st["overall_css"],
            "dstate": webdata.data_state_panel(cfg, db, now),
        })

    @app.get("/technical", response_class=HTMLResponse)
    def technical_page(request: Request):
        now = datetime.now(timezone.utc)
        data = webtechnical.technical_page_data(cfg, db, now)
        return templates.TemplateResponse(request, "technical.html", {
            "page": "technical", "data": data,
            **_market_ctx(cfg, db, now)})

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request, range: str = "7d",
                    status: str | None = None, source_type: str | None = None,
                    severity: str | None = None, page: int = 1):
        now = datetime.now(timezone.utc)
        data = webevents.events_page_data(
            cfg, db, now, range_key=range, status=status or None,
            source_type=source_type or None, severity=severity or None,
            page=max(1, page))
        return templates.TemplateResponse(request, "events.html", {
            "page": "events", "data": data, "tzname": str(cfg.local_tz),
            "range_options": data["range_options"],
            **_market_ctx(cfg, db, now)})

    @app.get("/events/{event_id}", response_class=HTMLResponse)
    def event_detail_page(request: Request, event_id: str):
        now = datetime.now(timezone.utc)
        data = webevents.event_detail_data(cfg, db, event_id, now)
        if data is None:
            return HTMLResponse("<h1>事件不存在</h1>"
                                "<p><a href=\"/events\">返回风险事件</a></p>",
                                status_code=404)
        return templates.TemplateResponse(request, "event_detail.html", {
            "page": "events", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/performance", response_class=HTMLResponse)
    def performance_page(request: Request, window: int = 5,
                         label: str | None = None, source_type: str | None = None):
        now = datetime.now(timezone.utc)
        data = webresearch.performance_page_data(
            cfg, db, now, window=window, label_code=label or None,
            source_type=source_type or None)
        return templates.TemplateResponse(request, "performance.html", {
            "page": "performance", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews_page(request: Request, status: str | None = None,
                     rule_code: str | None = None, page: int = 1):
        now = datetime.now(timezone.utc)
        data = webresearch.reviews_page_data(
            cfg, db, now, status=status or None, rule_code=rule_code or None,
            page=max(1, page))
        return templates.TemplateResponse(request, "reviews.html", {
            "page": "reviews", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/cashflow", response_class=HTMLResponse)
    def cashflow_page(request: Request, range: str = "30d",
                      category: str | None = None, page: int = 1,
                      anchor_page: int = 1):
        now = datetime.now(timezone.utc)
        data = webflex.page_data(cfg, db, now, range_key=range,
                                 category=category or None, page=max(1, page),
                                 anchor_page=max(1, anchor_page))
        return templates.TemplateResponse(request, "performance_cash.html", {
            "page": "cashflow", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/replay", response_class=HTMLResponse)
    def replay_page(request: Request, run: str | None = None):
        now = datetime.now(timezone.utc)
        data = webreplay.page_data(cfg, db, now, run_id=run or None)
        return templates.TemplateResponse(request, "replay.html", {
            "page": "replay", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/briefs", response_class=HTMLResponse)
    def briefs_page(request: Request, brief: str | None = None,
                    date: str | None = None):
        now = datetime.now(timezone.utc)
        data = webbriefs.page_data(cfg, db, now, brief_id=brief or None,
                                   date=date or None)
        return templates.TemplateResponse(request, "briefs.html", {
            "page": "briefs", "data": data, **_market_ctx(cfg, db, now)})

    @app.get("/reference", response_class=HTMLResponse)
    def reference(request: Request):
        now = datetime.now(timezone.utc)
        return templates.TemplateResponse(request, "reference.html", {
            "page": "reference",
            "pnl_bases": webdata.reconcile.PNL_BASES,
            "pnl_bases_warning": webdata.reconcile.PNL_BASES_WARNING,
            "rules": webdata.rule_reference(cfg),
            **_market_ctx(cfg, db, now),
        })

    @app.get("/calendar", response_class=HTMLResponse)
    def calendar_page(request: Request, year: int = 0, month: int = 0, view: str = "month"):
        now = datetime.now(timezone.utc)
        et = now.astimezone(cfg.market_tz)
        y = year or et.year
        m = month if 1 <= month <= 12 else et.month
        ctx = {"page": "calendar", "view": view, **_market_ctx(cfg, db, now)}
        if view == "year":
            ctx["cal_year"] = webdata.calendar_year_data(cfg, db, y, now)
        else:
            ctx["cal"] = webdata.calendar_data(cfg, db, y, m, now)
        return templates.TemplateResponse(request, "calendar.html", ctx)

    @app.get("/journal", response_class=HTMLResponse)
    def journal(request: Request):
        now = datetime.now(timezone.utc)
        return templates.TemplateResponse(request, "journal.html", {
            "page": "journal", "entries": webdata.journal_entries(cfg, db),
            **_market_ctx(cfg, db, now),
        })

    @app.get("/x", response_class=HTMLResponse)
    def x_feed(request: Request, handle: str | None = None, days: int = 7,
               feed_limit: int = 30, report_limit: int = 7):
        """X 推送：早报、推文流、关注博主。

        三块刻意分开呈现 —— 早报是 AI 成稿的结论，推文是博主原话，
        混在一起会让人分不清哪句是判断、哪句是原文。
        """
        now = datetime.now(timezone.utc)
        days = max(1, min(int(days or 7), 90))
        feed_limit = max(1, min(int(feed_limit or 30), 300))
        report_limit = max(1, min(int(report_limit or 7), 100))
        return templates.TemplateResponse(request, "x_feed.html", {
            "page": "x",
            "ov": x_service.overview(db),
            "feed": x_service.feed(db, days=days, limit=feed_limit,
                                   handle=handle or None, now=now),
            "reports": x_service.reports(db, limit=report_limit),
            "feed_limit": feed_limit, "report_limit": report_limit,
            "authors": x_service.authors(db),
            **_market_ctx(cfg, db, now),
        })

    @app.get("/x/feed-fragment", response_class=HTMLResponse)
    def x_feed_fragment(request: Request, handle: str | None = None, days: int = 7,
                        before: str | None = None, before_id: str | None = None,
                        loaded: int = 30):
        now = datetime.now(timezone.utc)
        days = max(1, min(int(days or 7), 90))
        batch = x_service.feed(db, days=days, limit=30, handle=handle or None,
                               now=now, before=before, before_id=before_id)
        return templates.TemplateResponse(request, "_x_feed_items.html", {
            "feed": batch, "loaded": max(0, loaded) + len(batch["tweets"]),
        })

    @app.get("/x/reports-fragment", response_class=HTMLResponse)
    def x_reports_fragment(request: Request, before_day: str | None = None,
                           loaded: int = 7):
        rows = x_service.reports(db, limit=7, before_day=before_day)
        return templates.TemplateResponse(request, "_x_report_items.html", {
            "reports": rows, "loaded": max(0, loaded) + len(rows),
            "report_has_more": len(rows) == 7,
        })

    async def _form(request: Request) -> dict:
        """解析 application/x-www-form-urlencoded 表单体（免 python-multipart 依赖）。"""
        raw = (await request.body()).decode("utf-8")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    async def _form_multi(request: Request) -> dict:
        """同 _form，但保留每个键的全部取值——操作计划是多行同名字段，需要逐行取回。"""
        raw = (await request.body()).decode("utf-8")
        return parse_qs(raw, keep_blank_values=True)

    def _plan_form_payload(form: dict) -> tuple[dict, str]:
        """把网页表单（键→取值列表）转换为结构化草稿；时间按网站配置时区转 UTC。"""
        payload = {k: v[0] for k, v in form.items()}
        intent = payload.pop("intent", "draft")
        review_at = payload.get("review_at")
        if review_at:
            try:
                parsed = datetime.fromisoformat(review_at)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=cfg.local_tz)
                payload["review_at"] = parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        # 操作计划：可增删的多行，同名字段按行平行提交，逐行还原成 action。
        # 加仓/减仓需填数量或触发条件才成立；平仓（EXIT）默认整仓，未填数量即按 100%。
        types = form.get("act_type", [])
        amounts = form.get("act_amount", [])
        units = form.get("act_unit", [])
        conditions = form.get("act_condition", [])
        actions = []
        for i, raw_type in enumerate(types):
            action_type = (raw_type or "").strip().upper()
            if action_type not in ("ADD", "REDUCE", "EXIT"):
                continue
            amount = (amounts[i] if i < len(amounts) else "").strip()
            percent = (units[i] if i < len(units) else "shares") == "percent"
            condition = (conditions[i] if i < len(conditions) else "").strip()
            if action_type == "EXIT" and not amount:
                actions.append({"action_type": "EXIT", "quantity": "",
                                "percentage": "100", "event_condition": condition})
            elif amount or condition:
                actions.append({
                    "action_type": action_type,
                    "quantity": amount if amount and not percent else "",
                    "percentage": amount if amount and percent else "",
                    "event_condition": condition,
                })
        # 各行的原始字段不进入草稿本体，避免污染计划字段。
        for key in ("act_type", "act_amount", "act_unit", "act_condition"):
            payload.pop(key, None)
        # 明确传 actions（可能为空）才能在编辑时清掉旧计划；为空即「仅持有，按期复盘」。
        payload["actions"] = actions
        payload["hold_only_review"] = "1" if not actions else ""
        # 超过最大仓位的二次确认只在发布被拦下后出现，不常驻表单。
        payload["limit_confirmed"] = "1" if intent == "publish_confirm" else ""
        return payload, ("publish" if intent == "publish_confirm" else intent)

    def _plan_error_redirect(base: str, cycle_id: str, message: str) -> RedirectResponse:
        """发布/保存失败时草稿已落库，带着错误回到原卡片，输入不丢。"""
        query = urlencode({"plan_error": cycle_id, "plan_error_msg": message})
        return RedirectResponse(f"{base}?{query}#pp-{cycle_id}", status_code=303)

    def _plan_error_ctx(request: Request) -> dict | None:
        cycle_id = request.query_params.get("plan_error")
        if not cycle_id:
            return None
        message = request.query_params.get("plan_error_msg") or "保存失败"
        # 只有仓位超限这一种错误可以二次确认后强行发布；缺必填项时该按钮不能出现
        # （缺项文案里也含「最大允许账户仓位」，不能用它做判断）。
        return {"cycle_id": cycle_id, "message": message,
                "limit_exceeded": "预计超过最大允许账户仓位" in message}

    def _web_plan_account(cycle_id: str | None = None) -> str | None:
        row = db.latest_snapshot_row()
        if row is not None and row["account"]:
            return row["account"]
        if cfg.ib_account:
            return cfg.ib_account
        if cycle_id:
            with db.connect() as conn:
                found = conn.execute(
                    "SELECT account FROM position_cycles WHERE id=?", (cycle_id,)
                ).fetchone()
                if found:
                    return found["account"]
        return None

    @app.get("/position-plans/{cycle_id}", response_class=HTMLResponse)
    def position_plan_detail_page(request: Request, cycle_id: str):
        now = datetime.now(timezone.utc)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("<h1>持仓计划不存在</h1>", status_code=404)
        try:
            detail = position_plan_service.PositionPlanService(
                db, account, actor="dashboard").detail(cycle_id)
        except position_plan_service.PositionPlanError:
            return HTMLResponse("<h1>持仓计划不存在或无权访问</h1>", status_code=404)
        return templates.TemplateResponse(request, "position_plan_detail.html", {
            "page": "overview", "detail": detail,
            "plan_error": _plan_error_ctx(request),
            **_market_ctx(cfg, db, now),
        })

    @app.post("/position-plans/{cycle_id}/quick")
    async def position_plan_quick(request: Request, cycle_id: str):
        form = await _form_multi(request)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        payload, intent = _plan_form_payload(form)
        payload.setdefault("change_reason", "NEW_CYCLE")
        try:
            service = position_plan_service.PositionPlanService(db, account, actor="dashboard")
            service.save_draft(cycle_id, payload, actor="dashboard")
            if intent == "publish":
                service.publish_version(cycle_id, actor="dashboard")
        except position_plan_service.PositionPlanError as exc:
            return _plan_error_redirect("/", cycle_id, str(exc))
        return RedirectResponse(f"/?open={cycle_id}#pp-{cycle_id}", status_code=303)

    @app.post("/position-plans/{cycle_id}/retry-journal")
    def position_plan_retry_journal(cycle_id: str):
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        try:
            position_plan_service.PositionPlanService(db, account).retry_archive(cycle_id)
        except position_plan_service.PositionPlanError as exc:
            return _plan_error_redirect("/", cycle_id, str(exc))
        return RedirectResponse("/#position-plans", status_code=303)

    @app.post("/position-plans/{cycle_id}/version")
    async def position_plan_version_form(request: Request, cycle_id: str):
        form = await _form_multi(request)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        payload, intent = _plan_form_payload(form)
        try:
            service = position_plan_service.PositionPlanService(db, account)
            service.save_draft(cycle_id, payload, actor="dashboard")
            if intent == "publish":
                service.publish_version(cycle_id, actor="dashboard")
        except position_plan_service.PositionPlanError as exc:
            return _plan_error_redirect(f"/position-plans/{cycle_id}", cycle_id, str(exc))
        return RedirectResponse(f"/position-plans/{cycle_id}", status_code=303)

    @app.post("/position-plans/{cycle_id}/actions/{action_id}/confirm")
    async def position_plan_action_confirm(request: Request, cycle_id: str, action_id: str):
        form = await _form(request)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        try:
            position_plan_service.PositionPlanService(db, account).confirm_action_intent(
                cycle_id, action_id,
                recommendation_id=form.get("recommendation_id") or None,
                note=form.get("note") or None, actor="dashboard")
        except position_plan_service.PositionPlanError as exc:
            return HTMLResponse(f"<h1>确认失败</h1><p>{str(exc)}</p>", status_code=400)
        return RedirectResponse(f"/position-plans/{cycle_id}", status_code=303)

    @app.post("/position-plans/{cycle_id}/executions/{execution_id}/confirm")
    async def position_plan_execution_confirm(
            request: Request, cycle_id: str, execution_id: str):
        form = await _form(request)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        try:
            position_plan_service.PositionPlanService(db, account).confirm_execution(
                cycle_id, execution_id, match_status=form.get("match_status") or "",
                action_id=form.get("action_id") or None,
                deviation_reason=form.get("deviation_reason") or None, actor="dashboard")
        except position_plan_service.PositionPlanError as exc:
            return HTMLResponse(f"<h1>确认失败</h1><p>{str(exc)}</p>", status_code=400)
        return RedirectResponse(f"/position-plans/{cycle_id}", status_code=303)

    @app.post("/position-plans/{cycle_id}/actions/{action_id}")
    async def position_plan_action_form(request: Request, cycle_id: str, action_id: str):
        form = await _form(request)
        account = _web_plan_account(cycle_id)
        if not account:
            return HTMLResponse("缺少账户标识", status_code=400)
        try:
            position_plan_service.PositionPlanService(db, account).update_action(
                cycle_id, action_id, status=form.get("status"), actor="dashboard")
        except position_plan_service.PositionPlanError as exc:
            return HTMLResponse(f"<h1>保存失败</h1><p>{str(exc)}</p>", status_code=400)
        return RedirectResponse(f"/position-plans/{cycle_id}", status_code=303)

    @app.post("/journal")
    async def journal_add(request: Request):
        f = await _form(request)
        content = (f.get("content") or "").strip()
        if content:
            db.add_journal_entry(content[:20000])      # 上限保护
        return RedirectResponse("/journal", status_code=303)

    @app.post("/journal/edit")
    async def journal_edit(request: Request):
        f = await _form(request)
        content = (f.get("content") or "").strip()
        try:
            eid = int(f.get("id", ""))
        except (TypeError, ValueError):
            eid = 0
        if eid and content:
            db.update_journal_entry(eid, content[:20000])
        return RedirectResponse("/journal", status_code=303)

    @app.post("/journal/delete")
    async def journal_delete(request: Request):
        f = await _form(request)
        try:
            eid = int(f.get("id", ""))
        except (TypeError, ValueError):
            eid = 0
        if eid:
            db.delete_journal_entry(eid)
        return RedirectResponse("/journal", status_code=303)

    @app.get("/history", response_class=HTMLResponse)
    def history(request: Request, symbol: str = "", action: str = "",
                start: str = "", end: str = "", page: int = 1):
        start_iso, end_iso = _bounds(start, end, cfg.market_tz)
        all_rows = webdata.history_rows(
            cfg, db, symbol=symbol or None, action=action or None,
            start=start_iso, end=end_iso,
        )
        page = max(1, page)
        per_page = 50
        total = len(all_rows)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        rows = all_rows[(page - 1) * per_page:page * per_page]
        f = {"symbol": symbol, "action": action, "start": start, "end": end}
        qs = urlencode({k: v for k, v in f.items() if v})
        now = datetime.now(timezone.utc)
        return templates.TemplateResponse(request, "history.html", {
            "page": "history", "rows": rows,
            "summary": webdata.history_summary(cfg, db, all_rows, start=start_iso,
                                               end=end_iso, now=now),
            "symbols": db.execution_symbols(), "f": f, "query_string": qs,
            "history_total": total, "history_page": page,
            "history_pages": pages,
            **_market_ctx(cfg, db, now),
        })

    @app.get("/history/export.csv")
    def export_csv(symbol: str = "", action: str = "", start: str = "", end: str = ""):
        start_iso, end_iso = _bounds(start, end, cfg.market_tz)
        csv_text = webdata.history_csv(
            cfg, db, symbol=symbol or None, action=action or None,
            start=start_iso, end=end_iso,
        )
        return Response(
            content=csv_text, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=transactions.csv"},
        )

    @app.get("/rules", response_class=HTMLResponse)
    def rules(request: Request):
        now = datetime.now(timezone.utc)
        return templates.TemplateResponse(request, "rules.html", {
            "page": "rules", "events": webdata.rule_events(cfg, db, now=now),
            "alerts": webdata.rule_alert_summary(cfg, db, now),
            "run": webdata.rule_run_status(cfg, db, now),
            **_market_ctx(cfg, db, now),
        })

    return app


def get_app() -> FastAPI:
    """uvicorn 工厂入口：uvicorn --factory app.webapp:get_app

    延迟到调用时才读 .env，避免仅 import 本模块（如测试）就要求完整配置。
    """
    cfg = load_config()
    db = Database(cfg.db_path)
    db.init_schema()
    # web 进程也需要 Notifier —— 只为「测试通知」端点。真实告警仍由 sync 进程发出，
    # 两个进程各自持有推送通道不会导致重复：事件通知只在 sync 侧的运营层触发。
    from .notify import Notifier
    return create_app(cfg, db, notifier=Notifier(cfg.apprise_urls, db))
