from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from app.futu_quotes import FutuQuoteClient


class _Context:
    def __init__(self, **_kwargs):
        self.calls = []

    def get_history_kl_quota(self, get_detail=False):
        return 0, (2, 98, [])

    def request_history_kline(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        page = kwargs.get("page_req_key")
        if page is None:
            return 0, [{"time_key": "2026-08-10 09:35:00", "open": 100,
                        "high": 101, "low": 99, "close": 100.5, "volume": 10}], b"next"
        return 0, [{"time_key": "2026-08-10 09:40:00", "open": 100.5,
                    "high": 102, "low": 100, "close": 101, "volume": 20}], None

    def close(self):
        pass


def test_futu_history_quota_and_paging(monkeypatch):
    fake_module = SimpleNamespace(
        AuType=SimpleNamespace(NONE="NONE"), KLType=SimpleNamespace(K_5M="K_5M"))
    monkeypatch.setitem(sys.modules, "futu", fake_module)
    cfg = SimpleNamespace(futu_host="127.0.0.1", futu_port=11111,
                          market_tz=__import__("zoneinfo").ZoneInfo("America/New_York"))
    context = _Context()
    client = FutuQuoteClient(cfg, context_factory=lambda **_kwargs: context)
    assert client.history_quota() == {"used": 2, "remaining": 98}
    rows = client.fetch_5m_bars(
        "NVDA", datetime(2026, 8, 10, tzinfo=timezone.utc),
        datetime(2026, 8, 11, tzinfo=timezone.utc))
    assert len(rows) == 2
    assert rows[0]["bar_start"] == "2026-08-10T13:30:00+00:00"
    assert rows[0]["bar_end"] == "2026-08-10T13:35:00+00:00"
    assert rows[0]["source"] == "FUTU_historical_5m"
    assert context.calls[0][1]["extended_time"] is False
    assert context.calls[1][1]["page_req_key"] == b"next"
