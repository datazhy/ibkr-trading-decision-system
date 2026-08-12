"""classify_quote 纯函数与隔夜行情补救（均无需 gateway）。"""
import datetime as dt
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.ibkr_client import IBKRClient, classify_quote

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def _q(**kw):
    base = dict(md_type=1, last=100.0, close=99.0, bid=99.5, ask=100.5,
                tick_time=NOW, halted=False, now=NOW, stale_seconds=90)
    base.update(kw)
    return classify_quote(**base)


def test_realtime_nonconsolidated():
    # 本账户免费行情：md_type=1 记为非综合实时
    q = _q(md_type=1)
    assert q.price == Decimal("100.0") and q.quality == "REALTIME_NONCONSOLIDATED"
    assert q.price_source == "LAST"
    assert q.as_of.startswith("2026-07-10T18:00")


def test_delayed():
    assert _q(md_type=3).quality == "DELAYED"


def test_frozen():
    assert _q(md_type=2).quality == "FROZEN"
    assert _q(md_type=4).quality == "DELAYED_FROZEN"   # 延迟冻结须与冻结区分


def test_halted_is_frozen():
    assert _q(md_type=1, halted=True).quality == "FROZEN"
    assert _q(md_type=1, halted=1.0).quality == "FROZEN"


def test_halted_nan_is_not_frozen():
    """回归：IBKR halted=nan(未知) 时 bool(nan)=True 会误判停牌，
    把正常延迟行情降级 FROZEN 导致 R7 永不触发。"""
    assert _q(md_type=3, halted=float("nan")).quality == "DELAYED"
    assert _q(md_type=1, halted=float("nan")).quality == "REALTIME_NONCONSOLIDATED"


def test_halted_zero_or_none_not_frozen():
    assert _q(md_type=3, halted=0).quality == "DELAYED"
    assert _q(md_type=3, halted=None).quality == "DELAYED"


def test_stale_overrides_realtime():
    # tick 时间 5 分钟前，超过 90s 阈值
    old = NOW - timedelta(minutes=5)
    assert _q(md_type=1, tick_time=old).quality == "STALE"


def test_stale_not_applied_to_frozen():
    old = NOW - timedelta(minutes=5)
    assert _q(md_type=2, tick_time=old).quality == "FROZEN"  # 冻结不因过期变 STALE


def test_invalid_price_zero():
    # close 也必须无效才是真 INVALID —— close 有效时会走收盘回退（见下方用例）
    q = _q(last=0, bid=0, ask=0, close=0)
    assert q.price is None and q.quality == "INVALID" and q.price_source == "NONE"


def test_invalid_price_nan():
    nan = float("nan")
    q = _q(last=nan, bid=nan, ask=nan, close=nan)
    assert q.price is None and q.quality == "INVALID" and q.price_source == "NONE"


def test_fallback_to_bid_ask_midpoint():
    q = _q(last=float("nan"), bid=99.0, ask=101.0, md_type=1)
    assert q.price == Decimal("100.0") and q.quality == "REALTIME_NONCONSOLIDATED"
    assert q.price_source == "BID_ASK_MID"   # 中点优先于 close


def test_delayed_maps_from_md_type_3():
    assert _q(md_type=3).quality == "DELAYED"


def test_missing_md_type():
    assert _q(md_type=None).quality == "MISSING"


def test_live_index_quote_uses_delayed_last_and_real_timestamp():
    client = IBKRClient.__new__(IBKRClient)
    client.cfg = SimpleNamespace(
        ib_market_data_type=3, futu_quote_stale_seconds=300)
    client._ensure_live_market_subscriptions = lambda: None
    delayed_time = NOW - timedelta(minutes=15)
    client._live_market_tickers = {"VIX": SimpleNamespace(
        marketDataType=3, last=15.45, close=14.9,
        bid=float("nan"), ask=float("nan"), time=delayed_time, halted=0)}
    quotes = client._live_index_market_quotes(NOW)
    assert quotes["VIX"]["price"] == Decimal("15.45")
    assert quotes["VIX"]["previous_close"] == Decimal("14.9")
    assert quotes["VIX"]["quality"] == "DELAYED"
    assert quotes["VIX"]["market_data_type"] == 3
    assert quotes["VIX"]["as_of"] == delayed_time.isoformat()


def test_live_index_quote_ignores_close_only_value():
    client = IBKRClient.__new__(IBKRClient)
    client.cfg = SimpleNamespace(
        ib_market_data_type=3, futu_quote_stale_seconds=300)
    client._ensure_live_market_subscriptions = lambda: None
    client._live_market_tickers = {"VIX": SimpleNamespace(
        marketDataType=3, last=-1, close=14.9,
        bid=float("nan"), ask=float("nan"), time=NOW, halted=0)}
    assert client._live_index_market_quotes(NOW) == {}


def test_live_index_subscriptions_recreate_once_per_connection_generation():
    class IB:
        def __init__(self):
            self.market_data_types = []
            self.qualified = []
            self.requested = []

        def reqMarketDataType(self, value):
            self.market_data_types.append(value)

        def qualifyContracts(self, contract):
            self.qualified.append(contract.symbol)

        def reqMktData(self, contract):
            self.requested.append(contract.symbol)
            return SimpleNamespace(contract=contract)

    ib = IB()
    client = IBKRClient.__new__(IBKRClient)
    client.cfg = SimpleNamespace(ib_market_data_type=3)
    client._connection_generation = 1
    client._live_market_subscription_generation = 0
    client._ensure_connected = lambda: ib

    client._ensure_live_market_subscriptions()
    client._ensure_live_market_subscriptions()
    assert ib.requested == ["VIX", "VXN", "TNX"]
    assert client._live_market_subscription_generation == 1

    client._connection_generation = 2
    client._ensure_live_market_subscriptions()
    assert ib.requested == ["VIX", "VXN", "TNX", "VIX", "VXN", "TNX"]
    assert client._live_market_subscription_generation == 2


# ---------------- 盘后收盘价回退（第三阶段） ----------------
# 实测：盘后 IBKR 返回 last=-1（哨兵）、bid/ask=nan，而 close 有效。
# 旧代码收 close 参数却从不使用 → price=None/INVALID → 首页拿 mv/qty 反推出一个
# 没有质量标记的「现价」，规则页同时说 INVALID，两页自相矛盾。

def test_close_fallback_after_hours_delayed():
    """盘后延迟行情：last=-1、bid/ask=nan → 用 close，标 DELAYED_FROZEN。"""
    nan = float("nan")
    q = _q(md_type=3, last=-1.0, bid=nan, ask=nan, close=19.14)
    assert q.price == Decimal("19.14")
    assert q.price_source == "CLOSE"
    assert q.quality == "DELAYED_FROZEN"


def test_close_fallback_has_no_as_of():
    """收盘价没有可信时点：绝不能拿 ticker.time 冒充。"""
    nan = float("nan")
    q = _q(md_type=3, last=-1.0, bid=nan, ask=nan, close=19.14, tick_time=NOW)
    assert q.as_of is None


def test_close_fallback_immune_to_fake_fresh_tick_time():
    """回归（防 R7 误报）：IBKR 盘后仍把 ticker.time 刷成当前时刻。
    若 close 回退后仍按 mdType 记 DELAYED 并参与新鲜度判定，就会把昨天的收盘价
    当成刚刚的实时价，让 R7 对着不动的收盘价发告警。"""
    nan = float("nan")
    fresh = NOW  # 伪装成「刚刚」的 tick 时间
    q = _q(md_type=3, last=-1.0, bid=nan, ask=nan, close=19.14,
           tick_time=fresh, now=NOW)
    assert q.quality == "DELAYED_FROZEN"      # 绝不能是 DELAYED
    assert q.as_of is None


def test_close_fallback_realtime_type_is_plain_frozen():
    nan = float("nan")
    q = _q(md_type=1, last=-1.0, bid=nan, ask=nan, close=19.14)
    assert q.quality == "FROZEN"              # 非延迟源的冻结不叫 DELAYED_FROZEN


def test_last_wins_over_close():
    q = _q(md_type=3, last=100.0, close=99.0)
    assert q.price == Decimal("100.0") and q.price_source == "LAST"


def test_negative_one_last_is_sentinel_not_price():
    """last=-1 是 IBKR 盘后哨兵，绝不能当成 -1 元的价格。"""
    nan = float("nan")
    q = _q(md_type=3, last=-1.0, bid=nan, ask=nan, close=50.0)
    assert q.price == Decimal("50.0")

# ==================== 隔夜行情补救 ====================

class _FakeTicker:
    def __init__(self, contract, *, last=None, close=None, bid=None, ask=None,
                 md_type=3, tick_time=None):
        self.contract = contract
        self.last, self.close, self.bid, self.ask = last, close, bid, ask
        self.marketDataType = md_type
        self.time = tick_time
        self.halted = 0


class _FakeContract:
    def __init__(self, symbol, exchange="SMART"):
        self.symbol, self.exchange = symbol, exchange


class _FakeIB:
    """记录每次 reqMktData 用的是哪个场内。"""

    def __init__(self, overnight_quotes=None):
        self.requested = []          # [(symbol, exchange)]
        self.cancelled = []
        self._overnight = overnight_quotes or {}

    def reqMktData(self, c):
        self.requested.append((c.symbol, c.exchange))
        if c.exchange != "SMART" and c.symbol in self._overnight:
            return _FakeTicker(c, **self._overnight[c.symbol])
        # SMART：隔夜午夜后只有收盘价
        return _FakeTicker(c, last=-1, close=100.0)

    def sleep(self, _s):
        pass

    def cancelMktData(self, c):
        self.cancelled.append((c.symbol, c.exchange))


def _client(cfg):
    from app import ibkr_client as ic
    obj = ic.IBKRClient.__new__(ic.IBKRClient)
    obj.cfg = cfg
    return obj


def _overnight_now():
    """ET 02:00 —— 隔夜后半段，实测正是回退收盘价的窗口。"""
    from zoneinfo import ZoneInfo
    return dt.datetime(2026, 7, 29, 2, 0,
                       tzinfo=ZoneInfo("America/New_York")).astimezone(
                           dt.timezone.utc)


def test_overnight_retry_recovers_live_price(make_config):
    """隔夜只拿到收盘价时，改走隔夜场拿到真实成交价。"""
    from app import ibkr_client as ic
    cfg = make_config(ib_account="U1", ib_quote_wait=0)
    cl = _client(cfg)
    ib = _FakeIB(overnight_quotes={"AAA": {"last": 123.45, "close": 100.0}})
    contracts = [_FakeContract("AAA")]
    out = {"AAA": {"price": None, "price_source": ic.PRICE_SOURCE_CLOSE}}

    fixed = cl._overnight_retry(ib, contracts, out, _overnight_now())
    assert fixed["AAA"]["price_source"] == ic.PRICE_SOURCE_LAST
    assert str(fixed["AAA"]["price"]) == "123.45"
    assert fixed["AAA"]["source"].endswith("@OVERNIGHT")
    # 确实定向到了隔夜场，且用完就释放
    assert ("AAA", "OVERNIGHT") in ib.requested
    assert ("AAA", "OVERNIGHT") in ib.cancelled


def test_overnight_retry_skips_outside_overnight(make_config):
    """只在隔夜时段执行 —— 盘中一律不碰，正常路径一行不动。"""
    from zoneinfo import ZoneInfo
    cfg = make_config(ib_account="U1", ib_quote_wait=0)
    cl = _client(cfg)
    ib = _FakeIB(overnight_quotes={"AAA": {"last": 123.45}})
    noon_et = dt.datetime(2026, 7, 29, 12, 0,
                          tzinfo=ZoneInfo("America/New_York")).astimezone(
                              dt.timezone.utc)
    from app import ibkr_client as ic
    out = {"AAA": {"price_source": ic.PRICE_SOURCE_CLOSE}}
    assert cl._overnight_retry(ib, [_FakeContract("AAA")], out, noon_et) == {}
    assert ib.requested == []


def test_overnight_retry_only_for_close_fallback(make_config):
    """已经有实时价的标的不重试 —— 不浪费行情线。"""
    from app import ibkr_client as ic
    cfg = make_config(ib_account="U1", ib_quote_wait=0)
    cl = _client(cfg)
    ib = _FakeIB(overnight_quotes={"AAA": {"last": 1.0}, "BBB": {"last": 2.0}})
    out = {"AAA": {"price_source": ic.PRICE_SOURCE_LAST},
           "BBB": {"price_source": ic.PRICE_SOURCE_CLOSE}}
    fixed = cl._overnight_retry(
        ib, [_FakeContract("AAA"), _FakeContract("BBB")], out, _overnight_now())
    assert set(fixed) == {"BBB"}
    assert [s for s, _e in ib.requested] == ["BBB"]


def test_overnight_retry_keeps_close_when_no_quote(make_config):
    """隔夜场同样没有报价时**维持收盘价** ——
    用另一个收盘价替换毫无意义，反而掩盖「隔夜确实没成交」这个事实。"""
    from app import ibkr_client as ic
    cfg = make_config(ib_account="U1", ib_quote_wait=0)
    cl = _client(cfg)
    ib = _FakeIB(overnight_quotes={"AAA": {"last": -1, "close": 100.0}})
    out = {"AAA": {"price_source": ic.PRICE_SOURCE_CLOSE}}
    assert cl._overnight_retry(ib, [_FakeContract("AAA")], out,
                               _overnight_now()) == {}


def test_overnight_retry_can_be_disabled(make_config):
    from app import ibkr_client as ic
    cfg = make_config(ib_account="U1", ib_quote_wait=0,
                      ib_overnight_quotes=False)
    cl = _client(cfg)
    ib = _FakeIB(overnight_quotes={"AAA": {"last": 1.0}})
    out = {"AAA": {"price_source": ic.PRICE_SOURCE_CLOSE}}
    assert cl._overnight_retry(ib, [_FakeContract("AAA")], out,
                               _overnight_now()) == {}
    assert ib.requested == []


def test_overnight_retry_failure_does_not_break_quotes(make_config):
    """订阅炸了也只是拿不到隔夜价，绝不让整轮行情丢失。"""
    from app import ibkr_client as ic

    class _Boom(_FakeIB):
        def reqMktData(self, c):
            raise RuntimeError("gateway 拒绝了隔夜场")

    cfg = make_config(ib_account="U1", ib_quote_wait=0)
    cl = _client(cfg)
    out = {"AAA": {"price_source": ic.PRICE_SOURCE_CLOSE}}
    assert cl._overnight_retry(_Boom(), [_FakeContract("AAA")], out,
                               _overnight_now()) == {}


# ==================== 免费非综合实时行情 + 回退 ====================

class _TypeAwareIB:
    """按「当前 reqMarketDataType」决定给不给数据的假 IB。

    模拟 IBKR 未订阅实时时的真实行为：**不抛异常、不报错，ticker 就是不填充**。
    错误码 10089 是异步 error 事件，同步调用侧看不到 —— 所以回退判据只能看结果。
    """

    def __init__(self, *, entitled_types=(3,), report_type=None):
        self.entitled = set(entitled_types)
        self.report_type = report_type      # None = 如实回报请求的类型
        self.type_calls = []
        self.requested = []
        self.cancelled = []
        self._cur = None

    def reqMarketDataType(self, t):
        self.type_calls.append(t)
        self._cur = t

    def reqMktData(self, c):
        self.requested.append((c.symbol, self._cur))
        reported = self.report_type if self.report_type is not None else self._cur
        if self._cur in self.entitled:
            return _FakeTicker(c, last=101.5, close=100.0, md_type=reported)
        # 未订阅：价格字段全空（IBKR 用 -1 / nan 表示无值）
        return _FakeTicker(c, last=-1, close=None, bid=None, ask=None,
                           md_type=None)

    def sleep(self, _s):
        pass

    def cancelMktData(self, c):
        self.cancelled.append(c.symbol)


def test_realtime_used_when_entitled(make_config):
    """订阅已生效：请求 type=1 就用 type=1，不触发回退。"""
    cfg = make_config(ib_account="U1", ib_quote_wait=0,
                      ib_market_data_type=1, ib_market_data_type_fallback=3,
                      ib_overnight_quotes=False)
    cl = _client(cfg)
    ib = _TypeAwareIB(entitled_types=(1,))
    # 走内部单次尝试，避开 _ensure_connected（不需要真连网关）
    out, eff = cl._try_market_data_type(ib, [_FakeContract("AAA")], 1)
    assert eff == 1
    assert out["AAA"]["price"] is not None
    assert ib.type_calls == [1]


def test_fallback_when_realtime_not_entitled(make_config):
    """**订阅未生效时必须回退，而不是把看板打瞎。**

    这是切换免费非综合实时的最大风险：请求 type=1 而未订阅，
    reqMktData 不报错，价格却一个都拿不到 —— 比原来的延迟价更糟。
    """
    from app import ibkr_client as ic
    ib = _TypeAwareIB(entitled_types=(3,))          # 只有延迟可用

    # 第一轮：请求实时 -> 一个价都没有
    out1, eff1 = _client(make_config(ib_quote_wait=0))._try_market_data_type(
        ib, [_FakeContract("AAA")], 1)
    assert ic._has_usable(out1) is False
    assert eff1 is None

    # 第二轮：回退延迟 -> 拿到价格
    out2, eff2 = _client(make_config(ib_quote_wait=0))._try_market_data_type(
        ib, [_FakeContract("AAA")], 3)
    assert ic._has_usable(out2) is True
    assert eff2 == 3
    assert out2["AAA"]["quality"] == "DELAYED"


def test_has_usable_treats_close_as_usable():
    """收盘价算「可用」—— 盘后本来就只有收盘价，不该因此误触发回退。"""
    from app import ibkr_client as ic
    from decimal import Decimal
    assert ic._has_usable({"A": {"price": Decimal("100")}}) is True
    assert ic._has_usable({"A": {"price": None}}) is False
    assert ic._has_usable({}) is False
    assert ic._has_usable(None) is False


def test_silent_downgrade_is_detected(make_config):
    """IBKR 可能**静默降级**：请求 1 却回报 3。这个差异必须留痕，
    否则会以为自己在用实时行情，实际还是延迟的。"""
    ib = _TypeAwareIB(entitled_types=(1,), report_type=3)
    out, eff = _client(make_config(ib_quote_wait=0))._try_market_data_type(
        ib, [_FakeContract("AAA")], 1)
    assert eff == 3                                  # 生效的是 3，不是请求的 1
    assert out["AAA"]["quality"] == "DELAYED"        # 质量如实按 3 分级


def test_market_data_lines_released_before_fallback(make_config):
    """回退前必须释放行情线，否则并发订阅数会翻倍。"""
    ib = _TypeAwareIB(entitled_types=(3,))
    cl = _client(make_config(ib_quote_wait=0))
    cl._try_market_data_type(ib, [_FakeContract("AAA"), _FakeContract("BBB")], 1)
    assert sorted(ib.cancelled) == ["AAA", "BBB"]


def test_nonconsolidated_quality_enables_r7_crit():
    """免费非综合实时（md_type=1）被视为实时档，R7 因此可以发 CRIT。

    这是切换的**副作用**，不是 bug：延迟价只配 WARN，实时价才配 CRIT。
    但它会改变告警等级，所以钉一条测试，避免以后有人以为是回归。
    """
    from app import ibkr_client as ic
    from app.rules import PriceQuality
    q = ic.classify_quote(md_type=1, last=100.0, close=99.0, bid=None, ask=None,
                          tick_time=dt.datetime.now(dt.timezone.utc),
                          halted=0, now=dt.datetime.now(dt.timezone.utc))
    assert q.quality == PriceQuality.REALTIME_NONCONSOLIDATED.value
    from app import rules
    assert PriceQuality.REALTIME_NONCONSOLIDATED in rules._R7_REALTIME


def test_status_bar_flags_realtime_not_effective(make_config):
    """状态栏必须把「请求了实时但没生效」说出来。

    否则最坏的情况是：订阅没开通、系统静静回退到延迟，而你以为已经是实时的，
    然后拿延迟价去判断止损距离。
    """
    from app.db import Database
    from app import webstatus
    cfg = make_config(ib_account="U1", ib_market_data_type=1)
    d = Database(cfg.db_path)
    d.init_schema()
    ts = "2026-07-30T15:00:00+00:00"
    d.write_account_snapshot(
        snapshot_id="s1", ts=ts, sync_run_id="r1", quality_status="fresh",
        ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": 40000,
                  "cash": 8000, "total_cash": 8000, "margin_balance": 8000,
                  "buying_power": 16000, "available_funds": 8000,
                  "excess_liquidity": 8000, "maint_margin": 1000,
                  "init_margin": 1200, "positions_value": 32000},
        positions=[{"symbol": "AAA", "qty": 100, "avg_cost": 300,
                    "market_value": 32000, "upl": 2000, "price": 320,
                    "price_quality": "DELAYED", "price_source": "LAST",
                    "market_data_type": 3,
                    "requested_market_data_type": 1,
                    "effective_market_data_type": 3}])
    bar = webstatus.data_status_bar(cfg, d)
    assert bar["detail"]["quote_downgraded"] is True
    assert bar["detail"]["quote_requested_type"] == [1]
    assert bar["detail"]["quote_effective_type"] == [3]
    quote_entry = next(e for e in bar["entries"] if e["label"] == "行情")
    assert "未生效" in quote_entry["extra"]


def test_status_bar_quiet_when_realtime_effective(make_config):
    """生效时不加噪音提示。"""
    from app.db import Database
    from app import webstatus
    cfg = make_config(ib_account="U1", ib_market_data_type=1)
    d = Database(cfg.db_path)
    d.init_schema()
    ts = "2026-07-30T15:00:00+00:00"
    d.write_account_snapshot(
        snapshot_id="s1", ts=ts, sync_run_id="r1", quality_status="fresh",
        ai_analysis_allowed=True,
        balances={"account": "U1", "currency": "USD", "equity": 40000,
                  "cash": 8000, "total_cash": 8000, "margin_balance": 8000,
                  "buying_power": 16000, "available_funds": 8000,
                  "excess_liquidity": 8000, "maint_margin": 1000,
                  "init_margin": 1200, "positions_value": 32000},
        positions=[{"symbol": "AAA", "qty": 100, "avg_cost": 300,
                    "market_value": 32000, "upl": 2000, "price": 320,
                    "price_quality": "REALTIME_NONCONSOLIDATED",
                    "price_source": "LAST", "market_data_type": 1,
                    "requested_market_data_type": 1,
                    "effective_market_data_type": 1}])
    bar = webstatus.data_status_bar(cfg, d)
    assert bar["detail"]["quote_downgraded"] is False
    quote_entry = next(e for e in bar["entries"] if e["label"] == "行情")
    assert "未生效" not in quote_entry["extra"]


# ---------------- 实时探测冷却 ----------------
# 回退本身已在上面覆盖。这里只管一件事：订阅未生效时**不要每轮都白等**
# 一个 ib_quote_wait，但也**不能永久放弃** —— 订阅一旦生效要能自动接上。

def _rt_cfg(make_config, **over):
    kw = dict(ib_account="U1", ib_quote_wait=0, ib_market_data_type=1,
              ib_market_data_type_fallback=3, ib_overnight_quotes=False)
    kw.update(over)
    return make_config(**kw)


def _rt_client(cfg, ib):
    from app.ibkr_client import IBKRClient
    c = IBKRClient.__new__(IBKRClient)
    c.cfg = cfg
    c._rt_probe_until = None
    c._ensure_connected = lambda: ib
    return c


def test_cooldown_skips_wasted_realtime_probe(make_config):
    """回退后进入冷却：下一轮直接用回退类型，不再白等实时。"""
    ib = _TypeAwareIB(entitled_types=(3,))
    c = _rt_client(_rt_cfg(make_config), ib)
    c._fetch_quotes([_FakeContract("AAA")])
    assert ib.type_calls == [1, 3]          # 首轮：探测 + 回退
    assert c._rt_probe_until is not None
    ib.type_calls.clear()
    c._fetch_quotes([_FakeContract("AAA")])
    assert ib.type_calls == [3]            # 冷却期内不再探测实时


def test_cooldown_expiry_resumes_probing(make_config):
    """冷却到期必须再探一次 —— 失败过不等于永久放弃。"""
    ib = _TypeAwareIB(entitled_types=(3,))
    c = _rt_client(_rt_cfg(make_config), ib)
    c._fetch_quotes([_FakeContract("AAA")])
    c._rt_probe_until = 0                  # 让冷却过期
    ib.type_calls.clear()
    c._fetch_quotes([_FakeContract("AAA")])
    assert ib.type_calls == [1, 3]


def test_entitlement_arriving_later_is_picked_up(make_config):
    """订阅生效后自动接上实时，不需要改配置也不需要重启。"""
    ib = _TypeAwareIB(entitled_types=(3,))
    c = _rt_client(_rt_cfg(make_config), ib)
    c._fetch_quotes([_FakeContract("AAA")])
    assert c._rt_probe_until is not None
    ib.entitled = {1}                      # 权限到位
    c._rt_probe_until = 0
    out = c._fetch_quotes([_FakeContract("AAA")])
    assert c._rt_probe_until is None       # 冷却解除
    assert out["AAA"]["effective_market_data_type"] == 1


def test_success_never_enters_cooldown(make_config):
    """一直有权限时不该被误置冷却。"""
    ib = _TypeAwareIB(entitled_types=(1,))
    c = _rt_client(_rt_cfg(make_config), ib)
    c._fetch_quotes([_FakeContract("AAA")])
    assert c._rt_probe_until is None
    assert ib.type_calls == [1]


def test_overnight_retry_keeps_market_data_type_stamps(make_config):
    """隔夜补价不得冲掉「请求 vs 生效」的留痕。

    补价是整条替换 dict 条目，若在补价前盖章就会被覆盖成 NULL ——
    线上会表现为「这几条没记录过行情类型」，而那正是判断订阅是否生效的凭据。
    """
    cfg = make_config(ib_account="U1", ib_quote_wait=0,
                      ib_market_data_type=1, ib_market_data_type_fallback=3,
                      ib_overnight_quotes=True, ib_overnight_exchange="OVERNIGHT")
    ib = _TypeAwareIB(entitled_types=(3,))
    from app.ibkr_client import IBKRClient
    c = IBKRClient.__new__(IBKRClient)
    c.cfg = cfg
    c._rt_probe_until = None
    c._ensure_connected = lambda: ib
    # 让补价返回一条替换条目（不含盖章字段），模拟真实覆盖
    c._overnight_retry = lambda *_a, **_k: {
        "AAA": {"price": Decimal("99"), "price_source": "LAST",
                "quality": "REALTIME_NONCONSOLIDATED", "as_of": None,
                "market_data_type": 1, "source": "IBKR_reqMktData@OVERNIGHT"}}
    out = c._fetch_quotes([_FakeContract("AAA")])
    assert out["AAA"]["source"].endswith("@OVERNIGHT")      # 补价确实生效了
    assert out["AAA"]["requested_market_data_type"] == 1    # 盖章没被冲掉
    assert out["AAA"]["effective_market_data_type"] == 3
