"""看板登录保护：口令哈希 + 无状态签名会话 Cookie。

设计取舍：
- **无状态会话**：Cookie 自带 `user|exp`，用服务端密钥 HMAC 签名。校验只看签名与
  过期时间，不查库、不存服务端会话表 —— web 进程重启、多副本都不会把用户踢下线，
  只要签名密钥稳定。密钥首次使用时随机生成并落库（sync_state），运维无需配置。
- **口令只存哈希**：PBKDF2-HMAC-SHA256，绝不落明文。哈希串自带算法/迭代数/盐，
  校验用 `compare_digest` 抗时序。改口令走 `manage.py webpass`，明文只在用户手里。
- 密钥与哈希都放 `sync_state`（明文键值即可 —— 哈希本就不可逆，密钥是随机串）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

_PBKDF2_ITERATIONS = 240_000
_SECRET_KEY = "web_session_secret"
_PWHASH_KEY = "web_auth_password_hash"
_COOKIE_NAME = "rk_session"


# ---------------- 口令哈希 ----------------

def hash_password(password: str) -> str:
    """返回 `pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>`。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                             _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"))


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt,
                                 int(iters_s))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, expected)


def set_password(db, password: str) -> None:
    db.set_sync_state(_PWHASH_KEY, hash_password(password))


def has_password(db) -> bool:
    return bool(db.get_sync_state(_PWHASH_KEY))


def check_credentials(cfg, db, user: str, password: str) -> bool:
    stored = db.get_sync_state(_PWHASH_KEY)
    if not stored:
        return False
    # 用户名也走定长比较，避免暴露「用户名对不对」的时序差异
    user_ok = hmac.compare_digest(user or "", cfg.dashboard_auth_user)
    pw_ok = verify_password(password or "", stored)
    return user_ok and pw_ok


# ---------------- 会话签名 ----------------

def _secret(db) -> bytes:
    raw = db.get_sync_state(_SECRET_KEY)
    if not raw:
        raw = secrets.token_urlsafe(48)
        db.set_sync_state(_SECRET_KEY, raw)
    return raw.encode("utf-8")


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(db, user: str, ttl_hours: int) -> str:
    exp = int(time.time()) + ttl_hours * 3600
    payload = "{}|{}".format(user, exp).encode("utf-8")
    sig = hmac.new(_secret(db), payload, hashlib.sha256).digest()
    return "{}.{}".format(_b64e(payload), _b64e(sig))


def verify_token(db, token: str | None) -> str | None:
    """返回合法会话的用户名，否则 None。"""
    if not token or "." not in token:
        return None
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(payload_b64)
        sig = _b64d(sig_b64)
    except (ValueError, TypeError):
        return None
    expected = hmac.new(_secret(db), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        user, exp_s = payload.decode("utf-8").split("|", 1)
        if int(exp_s) < int(time.time()):
            return None
    except (ValueError, UnicodeDecodeError):
        return None
    return user


# ---------------- Cookie 名 ----------------

COOKIE_NAME = _COOKIE_NAME
