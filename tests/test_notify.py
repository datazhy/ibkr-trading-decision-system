from datetime import timedelta

from app.db import Database
from app.notify import Notifier


class CountingNotifier(Notifier):
    def __init__(self, db):
        super().__init__([], db)
        self.count = 0

    def send(self, title, body):
        self.count += 1
        return True


def test_dedup_within_window(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    n = CountingNotifier(db)
    n.send_deduped("k", "t", "b", dedup_window=timedelta(hours=4))
    n.send_deduped("k", "t", "b", dedup_window=timedelta(hours=4))
    assert n.count == 1  # 窗口内只推一次


def test_dedup_different_keys(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    n = CountingNotifier(db)
    n.send_deduped("k1", "t", "b", dedup_window=timedelta(hours=4))
    n.send_deduped("k2", "t", "b", dedup_window=timedelta(hours=4))
    assert n.count == 2


def test_send_without_urls_returns_false(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.init_schema()
    n = Notifier([], db)  # 未配置 URL
    assert n.send("t", "b") is False
