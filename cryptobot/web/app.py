"""Flask app: dashboard UI, JSON state, live settings, and a kill switch.

Endpoints
  GET  /              -> the dashboard page
  GET  /api/state     -> full portfolio snapshot (stats, per-pair, charts data)
  GET  /api/config    -> current editable settings (for the settings form)
  POST /api/config    -> validate + apply + persist settings changes
  POST /api/stop      -> stop the trading engine (does not close positions)

The web layer never decides trades. Saving settings updates the shared config,
persists it to config.yaml, and asks the engine to reload — strategy/risk/
leverage changes take effect on the next poll.
"""

from __future__ import annotations

import logging
import threading

from flask import Flask, jsonify, render_template, request

from ..config import Config, save_config
from ..state import PortfolioState
from ..trader import TradingEngine

log = logging.getLogger("cryptobot.web")


def create_app(config: Config, state: PortfolioState,
               engine: TradingEngine | None,
               config_path: str = "config.yaml") -> Flask:
    app = Flask(__name__)
    settings_lock = threading.Lock()

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    @app.route("/api/config", methods=["GET"])
    def api_config_get():
        payload = config.editable_settings()
        # Fields the UI shows but that only take effect on restart.
        payload["_restart_required"] = ["market.symbols", "market.quote_currency"]
        payload["_has_api_keys"] = bool(config.exchange.api_key
                                        and config.exchange.api_secret)
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

    return app
