#!/usr/bin/env python3
"""IB Gateway watchdog —— 宿主机 systemd timer 每 60s 执行一次。

为什么不用 Docker HEALTHCHECK：容器可以显示 Up 而内部 Gateway 进程已死（实测挂 4 天）。
必须四层探活 + 受控重启：
  L1 容器 Running
  L2 gateway:4000 可 TCP 连接（容器网络内探测）
  L3 IB API 轻量握手（独立 clientId，只读，不碰认证）
  L4 数据新鲜度（最近账户快照 / 最近 rule_run 在阈值内）

恢复：只重启 gateway 容器 → 等现有 TOTP 自动登录 → resync 补同步 + 规则评估 → 恢复通知。
安全：日志与状态文件绝不含用户名/密码/TOTP；重启有冷却与每小时上限，超限停手并 CRIT 告警。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------- 配置（可用环境变量覆盖） ----------------
BASE = Path(os.environ.get("RISK_BASE", "/opt/risk-dashboard"))
GATEWAY = os.environ.get("WD_GATEWAY", "risk-ib-gateway")
SYNC = os.environ.get("WD_SYNC", "risk-sync")
DB_PATH = BASE / "data" / "dashboard.db"
STATE_PATH = BASE / "data" / "watchdog_state.json"
ENV_PATH = BASE / ".env"

GRACE_AFTER_RESTART = int(os.environ.get("WD_GRACE", "300"))       # 启动宽限 5 分钟
FAIL_THRESHOLD = int(os.environ.get("WD_FAIL_THRESHOLD", "3"))     # 连续失败 3 次
RESTART_COOLDOWN = int(os.environ.get("WD_COOLDOWN", "900"))       # 重启冷却 15 分钟
MAX_RESTARTS_PER_HOUR = int(os.environ.get("WD_MAX_PER_HOUR", "2"))
DATA_STALE_LIMIT = int(os.environ.get("WD_DATA_STALE", "300"))     # 数据新鲜度 5 分钟
LOGIN_WAIT = int(os.environ.get("WD_LOGIN_WAIT", "240"))           # 等 TOTP 登录最多 4 分钟

# 每日维护窗口（ET）：IBGA 每天 23:45 ET 自动 logoff 并关闭内部 gateway 进程，
# 属预期内事件。窗口内仍照常受控重启，但不推送例行「故障/恢复」通知以免每天刷屏；
# 恢复失败与 GIVE_UP 在窗口内**仍然告警**（那才是真问题）。
MAINT_SILENT = os.environ.get("WD_MAINT_SILENT", "1") == "1"
MAINT_START = os.environ.get("WD_MAINT_START", "23:40")            # ET
MAINT_END = os.environ.get("WD_MAINT_END", "00:15")                # ET（跨午夜）

# 更新窗口（ET 周日 20:00 → 周五 20:00 = 北京周一 08:00 → 周六 08:00）：
# 与 app 端 market_hours.is_update_window 同口径。**窗口外（周末）watchdog 完全休眠**——
# 周末美股休市、app 本就不同步，此时 IBGA 每日 logoff 后没有理由把它拉起来：
#   1. 数据新鲜度（L4）在周末必然「过期」，反复触发重启是**假故障驱动的无意义churn**，
#      每 60s 一次会持续冲击服务器、把 gateway 重启到 GIVE_UP，并刷满飞书。
#   2. 周末重启 gateway 也换不来任何新数据（app 不 sync）。
# 因此周末只记录「已暂停」状态、不探活、不重启、不推送。周日 20:00 ET 窗口重开后，
# 下一次运行自动探活并按需一次性恢复，供周一开盘使用。
WEEKEND_PAUSE = os.environ.get("WD_WEEKEND_PAUSE", "1") == "1"

# 更新窗口重开宽限期：周末暂停期间 IBGA 早已按每日 logoff 登出，窗口一重开必然探活
# 失败、必然要重启一次 —— 这是**预期内的例行恢复**，不该推送「Gateway 异常」。
# 窗口内仍照常重启，只是不发例行故障/恢复通知；真正的异常（重启失败/登录超时/
# 补同步失败/GIVE_UP）在本窗口内**依然告警**。
REOPEN_SILENT = os.environ.get("WD_REOPEN_SILENT", "1") == "1"
REOPEN_GRACE_MIN = int(os.environ.get("WD_REOPEN_GRACE_MIN", "60"))


# ---------------- 状态 ----------------

@dataclass
class WDState:
    state: str = "UNKNOWN"          # OK / FAILING / GIVEN_UP
    last_check: str | None = None
    last_ok: str | None = None
    fail_count: int = 0
    last_restart: str | None = None
    restarts: list[str] = field(default_factory=list)   # 最近重启时间（滑动窗口）
    notified_fault: bool = False
    notified_giveup: bool = False
    last_detail: str = ""

    @classmethod
    def load(cls, path: Path) -> "WDState":
        try:
            return cls(**json.loads(path.read_text()))
        except Exception:  # noqa: BLE001 — 状态文件损坏/首次运行
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2))
        tmp.replace(path)


# ---------------- 决策（纯函数，可单测） ----------------

ACT_NONE = "NONE"
ACT_RESTART = "RESTART"
ACT_GIVE_UP = "GIVE_UP"
ACT_RECOVERED = "RECOVERED"


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_window(cur_min: int, start_min: int, end_min: int) -> bool:
    """纯函数：cur 是否落在 [start, end] 内，支持跨午夜（start > end）。"""
    if start_min <= end_min:
        return start_min <= cur_min <= end_min
    return cur_min >= start_min or cur_min <= end_min


def _et_minutes(now: datetime) -> int:
    """把 UTC 时刻换算成美东当天的「分钟数」。

    宿主机 Python 3.8 无 zoneinfo；用 tzset 精确换算（自动跟随夏令时，
    避免把 23:45 ET 写死成固定 UTC 时刻而在 DST 切换后错位）。
    本脚本为单线程 oneshot，临时改 TZ 后立即还原。
    """
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        lt = time.localtime(now.timestamp())
        return lt.tm_hour * 60 + lt.tm_min
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def in_maintenance_window(now: datetime) -> bool:
    """当前是否处于 IBGA 每日 logoff 的预期维护窗口（ET）。"""
    if not MAINT_SILENT:
        return False
    return in_window(_et_minutes(now), _hhmm_to_min(MAINT_START), _hhmm_to_min(MAINT_END))


def _et_wday_min(now: datetime) -> tuple[int, int]:
    """ET 下的 (星期几, 当日分钟数)。星期一=0…星期日=6（与 datetime.weekday 一致）。"""
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        lt = time.localtime(now.timestamp())
        return lt.tm_wday, lt.tm_hour * 60 + lt.tm_min
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def in_update_window(now: datetime) -> bool:
    """是否在更新窗口内：ET 周日 20:00 → 周五 20:00（= 北京周一 08:00 → 周六 08:00）。

    与 app 端 market_hours.is_update_window 完全同口径。窗口外即周末休市。
    """
    wd, cur = _et_wday_min(now)
    post_close = 20 * 60                       # 20:00 ET
    if wd == 4 and cur >= post_close:          # 周五 20:00 之后
        return False
    if wd == 5:                                # 周六整天
        return False
    if wd == 6 and cur < post_close:           # 周日 20:00 之前
        return False
    return True


def in_reopen_grace(now: datetime) -> bool:
    """是否处于「更新窗口刚重开」的宽限期（ET 周日 20:00 起 REOPEN_GRACE_MIN 分钟）。

    周末停摆后 gateway 必定是登出状态，重开瞬间的探活失败与那一次重启都属例行，
    静默处理；仅静默**例行**故障/恢复通知，真正的失败仍会告警。
    """
    if not REOPEN_SILENT:
        return False
    wd, cur = _et_wday_min(now)
    start = 20 * 60                            # 周日 20:00 ET 窗口重开
    return wd == 6 and start <= cur < start + REOPEN_GRACE_MIN


def recent_restarts(restarts: list[str], now: datetime, window_s: int = 3600) -> list[str]:
    """滑动窗口内的重启记录。"""
    cut = now - timedelta(seconds=window_s)
    return [r for r in restarts if (_parse(r) or cut) > cut]


def decide(st: WDState, healthy: bool, now: datetime) -> tuple[str, str]:
    """返回 (action, reason)。纯函数：不做 IO。

    healthy=True  → 恢复（若之前故障）/保持 OK
    healthy=False → 累计失败；达阈值且不在宽限/冷却内且未超限 → RESTART；超限 → GIVE_UP
    """
    if healthy:
        if st.state in ("FAILING", "GIVEN_UP") or st.notified_fault:
            return ACT_RECOVERED, "健康检查恢复正常"
        return ACT_NONE, "healthy"

    fails = st.fail_count + 1
    if fails < FAIL_THRESHOLD:
        return ACT_NONE, f"连续失败 {fails}/{FAIL_THRESHOLD}，未达阈值"

    last_restart = _parse(st.last_restart)
    # 启动宽限：刚重启过，给 TOTP 登录时间，不重复重启
    if last_restart and (now - last_restart).total_seconds() < GRACE_AFTER_RESTART:
        return ACT_NONE, "处于启动宽限期，等待 TOTP 登录完成"
    # 冷却
    if last_restart and (now - last_restart).total_seconds() < RESTART_COOLDOWN:
        return ACT_NONE, "处于重启冷却期"
    # 每小时上限
    if len(recent_restarts(st.restarts, now)) >= MAX_RESTARTS_PER_HOUR:
        if st.state == "GIVEN_UP":
            return ACT_NONE, "已放弃自动重启，等待人工介入"
        return ACT_GIVE_UP, f"1 小时内已重启 {MAX_RESTARTS_PER_HOUR} 次，停止自动重启"
    return ACT_RESTART, f"连续失败 {fails} 次，执行受控重启"


# ---------------- 检查（IO） ----------------

def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def check_container() -> tuple[bool, str]:
    rc, out = _run(["docker", "inspect", "-f", "{{.State.Running}}", GATEWAY])
    ok = rc == 0 and "true" in out.lower()
    return ok, "L1 容器 Running" if ok else "L1 容器未运行"


def check_port() -> tuple[bool, str]:
    """在容器网络内探测 gateway:4000（宿主机无法直连 expose 端口）。"""
    code = ("import socket,sys\n"
            "s=socket.socket(); s.settimeout(5)\n"
            "try:\n"
            "    s.connect(('ib-gateway',4000)); print('OK')\n"
            "except Exception as e:\n"
            "    print('FAIL'); sys.exit(1)\n")
    rc, out = _run(["docker", "exec", "-i", SYNC, "python", "-c", code], timeout=20)
    ok = rc == 0 and "OK" in out
    return ok, "L2 端口 4000 可连接" if ok else "L2 端口 4000 不可连接"


def check_api() -> tuple[bool, str]:
    """IB API 轻量握手：独立 clientId、只读、不碰认证。"""
    code = ("from ib_async import IB\n"
            "ib=IB()\n"
            "try:\n"
            "    ib.connect('ib-gateway',4000,clientId=99,readonly=True,timeout=12)\n"
            "    a=ib.managedAccounts()\n"
            "    print('OK' if a else 'NOACC')\n"
            "finally:\n"
            "    try: ib.disconnect()\n"
            "    except Exception: pass\n")
    rc, out = _run(["docker", "exec", "-i", SYNC, "python", "-c", code], timeout=40)
    ok = rc == 0 and "OK" in out
    return ok, "L3 API 握手正常" if ok else "L3 API 握手失败"


def check_data_freshness(now: datetime) -> tuple[bool, str]:
    """L4：最近账户快照与最近 rule_run 是否在允许时间内。"""
    import sqlite3
    if not DB_PATH.exists():
        return False, "L4 数据库不存在"
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
        bal = conn.execute("SELECT ts FROM balances_snapshot ORDER BY ts DESC LIMIT 1").fetchone()
        run = conn.execute("SELECT run_ts FROM rule_runs ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        return False, f"L4 读库失败: {type(exc).__name__}"
    stale = []
    for label, row in (("账户快照", bal), ("rule_run", run)):
        if not row:
            stale.append(f"{label}无记录")
            continue
        age = (now - datetime.fromisoformat(row[0])).total_seconds()
        if age > DATA_STALE_LIMIT:
            stale.append(f"{label}滞后 {age/60:.1f} 分钟")
    if stale:
        return False, "L4 " + "、".join(stale)
    return True, "L4 数据新鲜"


def health_check(now: datetime) -> tuple[bool, str]:
    """四层依次检查，任一失败即不健康（短路，省资源）。"""
    for fn in (check_container, check_port, check_api):
        ok, detail = fn()
        if not ok:
            return False, detail
    ok, detail = check_data_freshness(now)
    if not ok:
        return False, detail
    return True, "L1-L4 全部正常"


# ---------------- 恢复 & 通知（IO） ----------------

def feishu_notify(title: str, text: str) -> bool:
    """直发飞书 webhook（从 .env 解析 token；绝不打印 token）。"""
    try:
        content = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return False
    m = re.search(r"^APPRISE_URLS=feishu://([A-Za-z0-9\-]+)", content, re.M)
    if not m:
        return False
    url = "https://open.feishu.cn/open-apis/bot/v2/hook/" + m.group(1)
    payload = json.dumps({"msg_type": "text",
                          "content": {"text": f"{title}\n{text}"}}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def restart_gateway() -> tuple[bool, str]:
    """只重启 gateway 容器（不动 db/web/sync）。"""
    rc, out = _run(["docker", "restart", GATEWAY], timeout=120)
    return rc == 0, ("gateway 已重启" if rc == 0 else f"重启失败: {out[:120]}")


def wait_for_login(now_fn=lambda: datetime.now(timezone.utc)) -> tuple[bool, str]:
    """等现有 TOTP 自动登录完成（轮询 L3 握手），不干预认证。"""
    deadline = time.time() + LOGIN_WAIT
    while time.time() < deadline:
        time.sleep(15)
        ok, _ = check_api()
        if ok:
            return True, "TOTP 自动登录完成，API 就绪"
    return False, f"等待登录超时（{LOGIN_WAIT}s）"


def resync() -> tuple[bool, str]:
    """恢复后补同步（账户/持仓/行情/订单/成交）+ 立即规则评估。只读。"""
    rc, out = _run(["docker", "exec", "-i", SYNC, "python", "manage.py", "resync"], timeout=180)
    ok = rc == 0 and "RESYNC_OK" in out
    tail = out.strip().splitlines()[-1][:120] if out.strip() else ""
    return ok, ("补同步完成" if ok else f"补同步失败: {tail}")


# ---------------- 主流程 ----------------

def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def publish_state(st: "WDState", now: datetime, healthy: bool, detail: str) -> None:
    """把 watchdog 状态写进 sync_state，供看板的系统健康详情读取。

    watchdog 跑在宿主机、看板跑在容器，两者共享同一个 sqlite 文件。这里只写
    几个 key，失败不影响 watchdog 本职（探活与恢复）。
    """
    import sqlite3
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        rows = [
            ("watchdog_last_check", now.isoformat()),
            ("watchdog_state", st.state),
            ("watchdog_healthy", "1" if healthy else "0"),
            ("watchdog_detail", detail[:200]),
            ("watchdog_fail_count", str(st.fail_count)),
            ("watchdog_restarts_24h", str(len(recent_restarts(st.restarts, now,
                                                              window_s=86400)))),
            ("watchdog_last_restart", st.last_restart or ""),
        ]
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO sync_state (key, value, updated_at) VALUES (?,?,?)",
                [(k, v, now.isoformat()) for k, v in rows],
            )
        conn.close()
    except Exception as exc:  # noqa: BLE001 — 看板可观测性不得拖垮探活主职
        log(f"写入 watchdog 状态失败（忽略）: {exc}")


def main() -> int:
    now = datetime.now(timezone.utc)
    st = WDState.load(STATE_PATH)

    # 周末休市：完全暂停探活/重启/推送。不做任何 IB API 握手（省资源、不打扰已 logoff
    # 的 gateway），只登记「已暂停」状态供看板显示。周日 20:00 ET 窗口重开后自动恢复。
    if WEEKEND_PAUSE and not in_update_window(now):
        st.last_check = now.isoformat()
        st.state = "PAUSED"
        st.fail_count = 0                      # 别把周五的失败数带进周一
        st.notified_fault = False
        st.last_detail = "休市窗口（周末）· watchdog 已暂停：不探活/不重启/不推送"
        publish_state(st, now, healthy=True, detail=st.last_detail)
        st.save(STATE_PATH)
        log("休市窗口，watchdog 暂停（周末不探活/不重启/不推送）")
        return 0

    healthy, detail = health_check(now)
    # 两种「预期内停摆」都静默例行通知：IBGA 每日 logoff、周末后窗口重开
    maint = in_maintenance_window(now) or in_reopen_grace(now)
    st.last_check = now.isoformat()
    st.last_detail = detail
    action, reason = decide(st, healthy, now)
    log(f"health={healthy} detail={detail} action={action} ({reason})"
        f"{' [例行窗口:通知静默]' if maint else ''}")
    publish_state(st, now, healthy, detail)

    def notify_failure(title: str, body: str) -> None:
        """故障类通知（含恢复失败）：去重一次；维护窗口内的『例行故障』才静默。"""
        if not st.notified_fault:
            feishu_notify(title, body)
            st.notified_fault = True
            log("已发送故障通知")

    if healthy:
        st.fail_count = 0
        st.last_ok = now.isoformat()
        if action == ACT_RECOVERED:
            # 只有此前真的报过故障（窗口外），才需要配一条恢复通知
            if st.notified_fault or st.notified_giveup:
                feishu_notify("✅ 小鱼交易台：IB Gateway 已恢复",
                              f"{detail}\n自动恢复流程完成，数据同步与规则评估已正常。")
                log("已发送恢复通知")
            else:
                log("维护窗口内例行恢复，静默")
        st.state = "OK"
        st.notified_fault = False
        st.notified_giveup = False
        st.save(STATE_PATH)
        return 0

    # 不健康
    st.fail_count += 1
    st.state = "FAILING"
    if st.fail_count >= FAIL_THRESHOLD and not maint:
        notify_failure("🔴 小鱼交易台：IB Gateway 异常",
                       f"{detail}\n连续失败 {st.fail_count} 次，watchdog 将尝试受控重启。")

    if action == ACT_RESTART:
        ok, msg = restart_gateway()
        st.last_restart = now.isoformat()
        st.restarts = recent_restarts(st.restarts, now) + [now.isoformat()]
        st.fail_count = 0          # 重启后重新计数，进入宽限期
        log(f"重启结果: {msg}")
        st.save(STATE_PATH)
        if not ok:
            # 重启本身失败 → 无论是否维护窗口都告警
            notify_failure("🔴 小鱼交易台：Gateway 重启失败", f"{detail}\n{msg}")
            st.save(STATE_PATH)
            return 0

        logged_in, lmsg = wait_for_login()
        log(f"登录等待: {lmsg}")
        if not logged_in:
            notify_failure("🔴 小鱼交易台：Gateway 重启后登录未完成",
                           f"{lmsg}\nTOTP 自动登录未在预期时间内就绪，请检查 ib-gateway。")
            st.save(STATE_PATH)
            return 0

        rok, rmsg = resync()
        log(f"补同步: {rmsg}")
        if not rok:
            notify_failure("🔴 小鱼交易台：Gateway 恢复后补同步失败",
                           f"{lmsg}\n{rmsg}\n请检查 sync 容器与数据同步。")
            st.save(STATE_PATH)
            return 0

        # 恢复成功：维护窗口内的例行恢复静默；窗口外（或已报过故障）才通知
        if maint and not st.notified_fault:
            log("维护窗口内例行重启恢复成功，静默（不推送）")
        else:
            feishu_notify("✅ 小鱼交易台：IB Gateway 已自动恢复",
                          f"{lmsg}\n{rmsg}\n规则评估已执行。")
            log("已发送恢复通知")
        st.state = "OK"
        st.fail_count = 0
        st.last_ok = datetime.now(timezone.utc).isoformat()
        st.notified_fault = False
        st.save(STATE_PATH)
        return 0

    if action == ACT_GIVE_UP:
        st.state = "GIVEN_UP"
        if not st.notified_giveup:
            feishu_notify("🔴🔴 小鱼交易台：Gateway 自动恢复已停止（需人工介入）",
                          f"{detail}\n1 小时内已自动重启 {MAX_RESTARTS_PER_HOUR} 次仍未恢复，"
                          f"已停止自动重启以免反复冲击。请手动检查 ib-gateway 容器。")
            st.notified_giveup = True
            log("已发送 GIVE_UP CRIT 告警")
    st.save(STATE_PATH)
    return 0


if __name__ == "__main__":
    sys.exit(main())
