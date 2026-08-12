"""看板登录保护：口令哈希、签名会话、整站中间件的端到端验证。"""
import hashlib
import hmac
import time

from fastapi.testclient import TestClient

from app import webauth
from app.db import Database
from app.webapp import create_app


def _client(make_config, **over):
    over.setdefault("dashboard_auth_enabled", True)
    cfg = make_config(ib_account="ACC", **over)
    db = Database(cfg.db_path)
    db.init_schema()
    return cfg, db, TestClient(create_app(cfg, db), follow_redirects=False)


# ---------- 口令哈希 ----------

def test_password_hash_roundtrip():
    h = webauth.hash_password("correct-horse")
    assert h.startswith("pbkdf2_sha256$")
    assert "correct-horse" not in h  # 不落明文
    assert webauth.verify_password("correct-horse", h)
    assert not webauth.verify_password("wrong", h)


# ---------- 中间件放行/拦截 ----------

def test_unauthenticated_redirects_to_login(make_config):
    _, _, c = _client(make_config)
    r = c.get("/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login?")
    assert "next=%2F" in r.headers["location"]


def test_login_page_open(make_config):
    _, _, c = _client(make_config)
    r = c.get("/login")
    assert r.status_code == 200 and "用户名" in r.text
    assert "小鱼交易台" in r.text
    assert "登录 · 小鱼交易台" in r.text


def test_healthz_open_without_auth(make_config):
    _, _, c = _client(make_config)
    assert c.get("/healthz").status_code == 200


def test_api_returns_401_not_redirect(make_config):
    _, _, c = _client(make_config)
    assert c.get("/api/v1/account/snapshot").status_code == 401


# ---------- 登录流 ----------

def test_wrong_password_rejected(make_config):
    _, db, c = _client(make_config)
    webauth.set_password(db, "correct-horse")
    r = c.post("/login", data={"username": "admin", "password": "nope", "next": "/"})
    assert r.status_code == 401 and "错误" in r.text


def test_login_then_access(make_config):
    _, db, c = _client(make_config)
    webauth.set_password(db, "correct-horse")
    r = c.post("/login", data={"username": "admin", "password": "correct-horse",
                               "next": "/status"})
    assert r.status_code == 303
    assert r.headers["location"] == "/status"
    tok = r.cookies[webauth.COOKIE_NAME]
    c.cookies.set(webauth.COOKIE_NAME, tok)
    assert c.get("/status").status_code == 200


def test_no_password_set_shows_hint(make_config):
    _, _, c = _client(make_config)
    r = c.post("/login", data={"username": "admin", "password": "x", "next": "/"})
    assert r.status_code == 503 and "webpass" in r.text


def test_open_redirect_blocked(make_config):
    _, db, c = _client(make_config)
    webauth.set_password(db, "pw123456")
    r = c.post("/login", data={"username": "admin", "password": "pw123456",
                               "next": "//evil.com"})
    assert r.headers["location"] == "/"


def test_logout_clears_cookie(make_config):
    _, db, c = _client(make_config)
    webauth.set_password(db, "pw123456")
    r = c.post("/login", data={"username": "admin", "password": "pw123456", "next": "/"})
    c.cookies.set(webauth.COOKIE_NAME, r.cookies[webauth.COOKIE_NAME])
    out = c.get("/logout")
    assert out.status_code == 303 and out.headers["location"] == "/login"


# ---------- 会话签名 ----------

def test_token_expiry_and_tamper(make_config):
    _, db, _ = _client(make_config)
    payload = f"admin|{int(time.time()) - 3600}".encode()
    sig = hmac.new(webauth._secret(db), payload, hashlib.sha256).digest()
    expired = f"{webauth._b64e(payload)}.{webauth._b64e(sig)}"
    assert webauth.verify_token(db, expired) is None            # 已过期
    assert webauth.verify_token(db, expired[:-4] + "AAAA") is None  # 签名被篡改
    good = webauth.issue_token(db, "admin", 24)
    assert webauth.verify_token(db, good) == "admin"


def test_auth_disabled_bypass(make_config):
    _, _, c = _client(make_config, dashboard_auth_enabled=False)
    assert c.get("/").status_code == 200
