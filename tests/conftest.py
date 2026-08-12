from zoneinfo import ZoneInfo

import pytest
from cryptography.fernet import Fernet

from app.config import Config


@pytest.fixture
def make_config(tmp_path):
    def _make(**overrides):
        defaults = dict(
            account_enc_key=Fernet.generate_key().decode(),
            db_path=tmp_path / "db.sqlite",
            log_path=tmp_path / "log.txt",
            sector_map_path=tmp_path / "sectors.json",
            market_tz=ZoneInfo("America/New_York"),
            local_tz=ZoneInfo("Asia/Shanghai"),
            positions_sync_interval=60,
            orders_sync_interval=300,
            apprise_urls=[],
            log_level="INFO",
            # 内容类测试与登录正交：默认关掉登录保护，避免每个页面断言都要先登录。
            # 登录保护本身由 tests/test_webauth.py 显式开启后单独验证。
            dashboard_auth_enabled=False,
        )
        defaults.update(overrides)
        return Config(**defaults)
    return _make
