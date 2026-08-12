"""富途主行情源与 IBKR 回退。"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.futu_quotes import FutuQuoteClient, quote_from_snapshot

ET = ZoneInfo("America/New_York")


def test_pre_market_snapshot_is_realtime():
    now = datetime(2026, 7, 31, 10, 39, 30, tzinfo=timezone.utc)  # 06:39 ET
    row = {"update_time": "2026-07-31 06:39:26.299",
           "pre_price": 197.9, "last_price": 183.3, "prev_close_price": 195}
    q = quote_from_snapshot(row, now=now, market_tz=ET, stale_seconds=300)
    assert q["price"] == Decimal("197.9")
    assert q["price_source"] == "PRE_MARKET"
    assert q["quality"] == "REALTIME"
    assert q["source"] == "FUTU_OpenD"
    assert q["previous_close"] == Decimal("195")


def test_session_uses_matching_price_field():
    regular = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)  # 10:00 ET
    row = {"update_time": "2026-07-31 10:00:00", "last_price": 101,
           "pre_price": 99, "after_price": 102, "overnight_price": 98}
    q = quote_from_snapshot(row, now=regular, market_tz=ET, stale_seconds=300)
    assert q["price"] == Decimal("101") and q["price_source"] == "LAST"


def test_stale_futu_quote_is_rejected_for_ibkr_fallback():
    now = datetime(2026, 7, 31, 14, 10, tzinfo=timezone.utc)
    old = (now.astimezone(ET) - timedelta(minutes=10)).replace(tzinfo=None).isoformat(" ")
    assert quote_from_snapshot(
        {"update_time": old, "last_price": 101}, now=now,
        market_tz=ET, stale_seconds=300) is None


def test_closed_market_uses_futu_close_without_claiming_realtime():
    saturday = datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc)
    q = quote_from_snapshot(
        {"update_time": "2026-07-31 16:00:00", "last_price": 101,
         "prev_close_price": 99}, now=saturday, market_tz=ET, stale_seconds=300)
    assert q["price"] == Decimal("101")
    assert q["previous_close"] == Decimal("99")
    assert q["price_source"] == "CLOSE"
    assert q["quality"] == "DELAYED_FROZEN"
    assert q["source"] == "FUTU_OpenD"


def test_closed_market_current_keeps_cached_futu_quote(make_config):
    saturday = datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc)
    client = FutuQuoteClient(make_config(futu_quote_enabled=True))
    client._rows_by_code = {"US.AAA": {
        "code": "US.AAA", "update_time": "2026-07-31 16:00:00",
        "last_price": 101, "prev_close_price": 99,
    }}
    out = client.current(["AAA"], now=saturday, ensure_started=False)
    assert out["AAA"]["price"] == Decimal("101")
    assert out["AAA"]["previous_close"] == Decimal("99")
    assert out["AAA"]["quality"] == "DELAYED_FROZEN"


def test_position_quotes_futu_first_and_ibkr_per_symbol_fallback(make_config):
    from app.ibkr_client import IBKRClient

    class Primary:
        def fetch(self, symbols):
            assert symbols == ["AAA", "BBB"]
            return {"AAA": {"price": Decimal("10"), "source": "FUTU_OpenD"}}

    client = IBKRClient.__new__(IBKRClient)
    client.cfg = make_config(futu_quote_enabled=True)
    client._futu_quote_client = Primary()
    seen = []

    def fallback(contracts):
        seen.extend(c.symbol for c in contracts)
        return {"BBB": {"price": Decimal("9"), "source": "IBKR_reqMktData"}}

    client._fetch_quotes = fallback
    result = client._fetch_position_quotes([
        SimpleNamespace(symbol="AAA"), SimpleNamespace(symbol="BBB")])
    assert seen == ["BBB"]
    assert result["AAA"]["source"] == "FUTU_OpenD"
    assert result["BBB"]["source"] == "IBKR_reqMktData"


def test_position_quotes_full_ibkr_fallback_when_futu_fails(make_config):
    from app.ibkr_client import IBKRClient

    class FailedPrimary:
        def fetch(self, symbols):
            return {}

    client = IBKRClient.__new__(IBKRClient)
    client.cfg = make_config(futu_quote_enabled=True)
    client._futu_quote_client = FailedPrimary()
    client._fetch_quotes = lambda contracts: {
        c.symbol: {"price": Decimal("8"), "source": "IBKR_reqMktData"}
        for c in contracts}
    result = client._fetch_position_quotes([SimpleNamespace(symbol="AAA")])
    assert result["AAA"]["source"] == "IBKR_reqMktData"


def test_quote_status_distinguishes_source_and_realtime():
    from app.webdata import _quote_status_summary
    now = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)
    rows = [
        {"price": Decimal("10"), "quote_is_realtime": True,
         "quote_source_label": "富途", "quote_as_of": now.isoformat()},
        {"price": Decimal("9"), "quote_is_realtime": False,
         "quote_source_label": "IBKR", "quote_as_of": None},
    ]
    status = _quote_status_summary(rows, now, "REGULAR")
    assert status["label"] == "部分实时 · 已回退"
    assert status["futu_live"] == 1 and status["ibkr_fallback"] == 1


def test_futu_push_cache_updates_without_repeated_snapshot(make_config):
    class Frame:
        def __init__(self, rows): self.rows = rows
        def to_dict(self, orient):
            assert orient == "records"
            return self.rows

    class Context:
        def __init__(self):
            self.snapshot_calls = 0
            self.subscribe_calls = []
        def subscribe(self, codes, subtypes, **kwargs):
            self.subscribe_calls.append((codes, subtypes, kwargs))
            return 0, "ok"
        def unsubscribe(self, codes, subtypes): return 0, "ok"
        def get_market_snapshot(self, codes):
            self.snapshot_calls += 1
            return 0, Frame([{"code": "US.AAA", "update_time": "2026-07-31 10:00:00",
                              "last_price": 10}])
        def close(self): pass

    cfg = make_config(futu_quote_enabled=True)
    ctx = Context()
    client = FutuQuoteClient(cfg, context_factory=lambda **kwargs: ctx)
    now = datetime(2026, 7, 31, 14, 0, 1, tzinfo=timezone.utc)
    assert client.start(["AAA"])
    assert ctx.subscribe_calls[0][2]["subscribe_push"] is True
    assert client.current(["AAA"], now=now)["AAA"]["price"] == Decimal("10")
    # 模拟 OpenD 推送；读取 current 不再请求 snapshot。
    client._accept_rows(Frame([{"code": "US.AAA", "update_time": "2026-07-31 10:00:01",
                                "last_price": 10.25}]))
    assert client.current(["AAA"], now=now)["AAA"]["price"] == Decimal("10.25")
    assert ctx.snapshot_calls == 1


def test_push_data_date_time_replaces_initial_snapshot_time(make_config):
    """QUOTE 推送没有 update_time；必须用 data_date/data_time 推进行情时点。"""
    cfg = make_config(futu_quote_enabled=True)
    client = FutuQuoteClient.__new__(FutuQuoteClient)
    client.cfg = cfg
    client._rows_by_code = {
        "US.AAA": {"code": "US.AAA", "update_time": "2026-07-31 10:00:00",
                   "last_price": 10, "prev_close_price": 9}}
    received = datetime(2026, 7, 31, 14, 0, 6, tzinfo=timezone.utc)
    client._accept_rows({"code": "US.AAA", "data_date": "2026-07-31",
                         "data_time": "10:00:05.250", "last_price": 10.25},
                        received_at=received)
    row = client._rows_by_code["US.AAA"]
    assert row["update_time"] == "2026-07-31 10:00:05.250"
    q = quote_from_snapshot(
        row, now=datetime(2026, 7, 31, 14, 0, 6, tzinfo=timezone.utc),
        market_tz=ET, stale_seconds=300)
    assert q["price"] == Decimal("10.25")
    assert q["as_of"] == "2026-07-31T14:00:05.250000+00:00"


def test_extended_hours_push_uses_feed_receive_time(make_config):
    """盘前推送的 data_time 是昨日收盘时间，不能把新盘前价判过期。"""
    cfg = make_config(futu_quote_enabled=True)
    client = FutuQuoteClient.__new__(FutuQuoteClient)
    client.cfg = cfg
    client._rows_by_code = {
        "US.QQQ": {"code": "US.QQQ", "update_time": "2026-07-31 08:09:59",
                   "pre_price": 690, "last_price": 683.55}}
    received = datetime(2026, 7, 31, 12, 10, 23, tzinfo=timezone.utc)  # 08:10 ET
    client._accept_rows({"code": "US.QQQ", "data_date": "2026-07-30",
                         "data_time": "16:00:00.688", "pre_price": 690.14},
                        received_at=received)
    row = client._rows_by_code["US.QQQ"]
    assert row["update_time"] == received.isoformat()
    q = quote_from_snapshot(
        row, now=received + timedelta(seconds=1), market_tz=ET,
        stale_seconds=300)
    assert q["price"] == Decimal("690.14")
    assert q["as_of"] == received.isoformat()
