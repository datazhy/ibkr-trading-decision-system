from scripts.deploy_preflight import _site_error


def test_deploy_preflight_rejects_placeholder_and_port_sites():
    assert _site_error("dash.example.com:8856") == "DASHBOARD_SITE_MUST_NOT_HAVE_PORT"
    assert _site_error("dash.example.com") == "DASHBOARD_SITE_PLACEHOLDER"


def test_deploy_preflight_accepts_real_hostname():
    assert _site_error("dashboard.example.net") is None
    assert _site_error("https://dashboard.example.net") == "DASHBOARD_SITE_INVALID"
