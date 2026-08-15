"""Flask app serving the dashboard UI and a JSON state endpoint.

The web layer is read-only with respect to trading: it never places orders. It
exposes the engine's shared state as JSON and the dashboard polls it. The one
control it offers is a graceful shutdown of the trading loop (a kill switch).
"""

from __future__ import annotations

from flask import Flask, jsonify, render_template

from ..state import BotState
from ..trader import TradingEngine


def create_app(state: BotState, engine: TradingEngine) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/state")
    def api_state():
        return jsonify(state.snapshot())

    @app.route("/api/stop", methods=["POST"])
    def api_stop():
        """Kill switch — stop the trading engine (does not close positions)."""
        engine.stop()
        return jsonify({"ok": True, "status": state.snapshot()["status"]})

    return app
