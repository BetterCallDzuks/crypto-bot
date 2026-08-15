"""Dashboard authentication and the exposure guard."""

import pytest
from werkzeug.security import generate_password_hash

from cryptobot.config import AuthConfig, Config, WebConfig
from cryptobot.state import PortfolioState
from cryptobot.web.app import create_app


def _state():
    return PortfolioState(bases=["BTC"], symbols={"BTC": "BTC/USDC:USDC"},
                          starting_balance=10_000)


def _auth(password="secret"):
    return AuthConfig(username="admin",
                      password_hash=generate_password_hash(password),
                      secret_key="test-secret-key")


def _client(config):
    app = create_app(config, _state(), None)
    app.config.update(TESTING=True)
    return app.test_client()


# -- exposure guard (validate) --------------------------------------------
def test_nonlocal_host_without_password_refused():
    cfg = Config(web=WebConfig(host="0.0.0.0", auth_enabled=True))
    with pytest.raises(ValueError, match="without authentication"):
        cfg.validate()


def test_nonlocal_host_with_password_ok():
    cfg = Config(web=WebConfig(host="0.0.0.0", auth_enabled=True),
                 auth=_auth())
    cfg.validate()                      # should not raise
    assert cfg.auth_active is True


def test_localhost_without_password_allowed_open():
    cfg = Config(web=WebConfig(host="127.0.0.1", auth_enabled=True))
    cfg.validate()                      # localhost may run open
    assert cfg.auth_active is False


# -- request gating --------------------------------------------------------
def test_open_dashboard_serves_without_login():
    cfg = Config()                      # no password -> auth inactive
    client = _client(cfg)
    assert client.get("/api/state").status_code == 200


def test_protected_api_returns_401_without_session():
    cfg = Config(auth=_auth())
    client = _client(cfg)
    assert client.get("/api/state").status_code == 401


def test_protected_page_redirects_to_login():
    cfg = Config(auth=_auth())
    client = _client(cfg)
    r = client.get("/")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_wrong_password_rejected():
    cfg = Config(auth=_auth())
    client = _client(cfg)
    r = client.post("/login", data={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    assert client.get("/api/state").status_code == 401   # still locked out


def test_correct_login_grants_access():
    cfg = Config(auth=_auth("hunter2"))
    client = _client(cfg)
    r = client.post("/login", data={"username": "admin", "password": "hunter2"})
    assert r.status_code == 302
    assert client.get("/api/state").status_code == 200   # session works

    client.get("/logout")
    assert client.get("/api/state").status_code == 401   # logged out again
