"""Flask app: dashboard UI, JSON state, live settings, kill switch, and auth.

Endpoints
  GET  /              -> the dashboard page
  GET  /login         -> login form (only when auth is active)
  POST /login         -> authenticate, start a session
  GET  /logout        -> end the session
  GET  /api/state     -> full portfolio snapshot
  GET  /api/config    -> current editable settings
  POST /api/config    -> validate + apply + persist settings changes
  POST /api/stop      -> stop the trading engine (does not close positions)

When ``config.auth_active`` is true (auth enabled AND a password configured),
every route except the login page and static assets requires a logged-in
session. API calls made without a session get 401 so the dashboard can bounce
the user to /login.
"""

from __future__ import annotations

import logging
import threading

from flask import (
    Flask, jsonify, redirect, render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash

from ..config import Config, save_config
from ..state import PortfolioState
from ..trader import TradingEngine

log = logging.getLogger("cryptobot.web")


def create_app(config: Config, state: PortfolioState,
               engine: TradingEngine | None,
               config_path: str = "config.yaml") -> Flask:
    app = Flask(__name__)
    app.secret_key = config.auth.secret_key
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    settings_lock = threading.Lock()

    # -- authentication gate ----------------------------------------------
    @app.before_request
    def _require_login():
        if not config.auth_active:
            return None
        if request.endpoint == "login" or request.path == "/login":
            return None
        if (request.endpoint or "").endswith("static"):
            return None
        if session.get("auth"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not config.auth_active:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            user = request.form.get("username", "")
            pw = request.form.get("password", "")
            if (user == config.auth.username
                    and check_password_hash(config.auth.password_hash, pw)):
                session["auth"] = True
                session.permanent = True
                return redirect(url_for("index"))
            error = "Invalid username or password."
            log.warning("Failed dashboard login for user %r", user)
        return render_template("login.html", error=error), (401 if error else 200)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login") if config.auth_active
                        else url_for("index"))

    # -- dashboard + API ---------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html", auth_active=config.auth_active)

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        from ..strategy import available_strategies
        payload = config.editable_settings()
        payload["_restart_required"] = ["market.symbols", "market.quote_currency"]
        payload["_has_api_keys"] = bool(config.exchange.api_key
                                        and config.exchange.api_secret)
        payload["_strategies"] = available_strategies()
        return jsonify(payload)

    @app.route("/api/config", methods=["POST"])
    def api_config_post():
        updates = request.get_json(silent=True) or {}
        with settings_lock:
            try:
                config.apply_updates(updates)
            except Exception as exc:  # noqa: BLE001 - reported to the client
                return jsonify({"ok": False, "error": str(exc)}), 400
            try:
                save_config(config, config_path)
            except Exception as exc:  # noqa: BLE001
                log.warning("Settings applied but could not be saved: %s", exc)
            if engine is not None:
                engine.reload_from_config()
            state.update_meta(config.market.quote_currency,
                              config.futures.enabled,
                              config.futures.leverage if config.futures.enabled
                              else 1)
        log.info("Settings updated via dashboard: %s", updates)
        return jsonify({"ok": True, "settings": config.editable_settings()})

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        if engine is not None:
            engine.stop()
        return jsonify({"ok": True, "status": state.snapshot()["status"]})

    # -- backtesting -------------------------------------------------------
    @app.route("/api/backtest", methods=["POST"])
    def api_backtest():
        import copy as _copy

        from ..backtest import load_history, run_backtest
        body = request.get_json(silent=True) or {}
        bars = max(200, min(int(body.get("bars", 1500)), 5000))
        fee, slip, fund = _cost_params(body, config)
        cfg = _copy.deepcopy(config)
        if body.get("strategy"):
            cfg.strategy.name = str(body["strategy"])
            cfg.strategy.params = {}
        try:
            cfg.validate()
            history = load_history(cfg, bars)
            result = run_backtest(cfg, history, fee_rate=fee, slippage_rate=slip,
                                  funding_rate=fund)
        except Exception as exc:  # noqa: BLE001 - reported to the client
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "result": result,
                        "source": cfg.market.source})

    @app.route("/api/backtest/compare", methods=["POST"])
    def api_backtest_compare():
        from ..backtest import compare_strategies, load_history
        body = request.get_json(silent=True) or {}
        bars = max(200, min(int(body.get("bars", 1500)), 5000))
        fee, slip, fund = _cost_params(body, config)
        try:
            history = load_history(config, bars)
            results = compare_strategies(config, history, fee_rate=fee,
                                         slippage_rate=slip, funding_rate=fund)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "results": results,
                        "source": config.market.source})

    @app.route("/api/backtest/walkforward", methods=["POST"])
    def api_backtest_walkforward():
        from ..backtest import load_history, walk_forward
        body = request.get_json(silent=True) or {}
        bars = max(200, min(int(body.get("bars", 3000)), 5000))
        folds = max(1, min(int(body.get("folds", 4)), 10))
        objective = str(body.get("objective", "profit_factor"))
        fee, slip, fund = _cost_params(body, config)
        try:
            history = load_history(config, bars)
            result = walk_forward(config, history, folds=folds,
                                  objective=objective, fee_rate=fee,
                                  slippage_rate=slip, funding_rate=fund)
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, **result, "source": config.market.source})

    return app


def _cost_params(body: dict, config: Config) -> tuple[float, float, float]:
    """Read fee/slippage/funding % from the request body, defaulting to config."""
    def pct(key: str, default: float, floor: float = 0.0) -> float:
        if key in body and body[key] not in (None, ""):
            return max(floor, float(body[key]) / 100.0)   # UI sends percent
        return default
    return (pct("fee_pct", config.backtest.fee_rate),
            pct("slippage_pct", config.backtest.slippage_rate),
            pct("funding_pct", config.backtest.funding_rate, floor=-1.0))
