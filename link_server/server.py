"""
Minimal Flask server for the Plaid Link browser flow.

Usage (normal link):
    python -m link_server.server

Usage (reauth/update mode):
    REAUTH_ITEM_ID=<item_id> python -m link_server.server

The server exposes:
  GET  /           — HTML page embedding Plaid Link JS
  POST /api/link_token   — returns {link_token}
  POST /api/exchange     — exchanges public_token, persists to DB, returns {ok, institution}
"""

import os
import signal
import sys
import threading

# Allow running as __main__ from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify, render_template, request

from fintrack.config import get_settings
from fintrack.db import configure_encryption, get_connection, insert_item, migrate
from fintrack.plaid_client import (
    create_client,
    create_link_token,
    create_update_link_token,
    exchange_public_token,
    get_institution_name,
)

app = Flask(__name__)

_settings = get_settings()
app.secret_key = _settings.flask_secret_key
configure_encryption(_settings.fernet_key)

_plaid_client = create_client(
    _settings.plaid_client_id,
    _settings.plaid_secret,
    _settings.plaid_env,
)

_reauth_item_id: str | None = os.environ.get("REAUTH_ITEM_ID")


@app.get("/")
def index():
    return render_template("link.html", reauth=bool(_reauth_item_id))


@app.post("/api/link_token")
def api_link_token():
    try:
        if _reauth_item_id:
            conn = get_connection(_settings.db_path)
            try:
                row = conn.execute(
                    "SELECT access_token FROM items WHERE item_id = ?",
                    (_reauth_item_id,),
                ).fetchone()
            finally:
                conn.close()

            if not row:
                return jsonify({"error": f"Item '{_reauth_item_id}' not found"}), 404

            token = create_update_link_token(
                _plaid_client,
                access_token=row["access_token"],
                client_user_id="fintrack-user",
                client_name=_settings.plaid_client_name,
                link_customization_name=_settings.link_customization_name,
            )
        else:
            token = create_link_token(
                _plaid_client,
                client_user_id="fintrack-user",
                client_name=_settings.plaid_client_name,
                link_customization_name=_settings.link_customization_name,
                products=_settings.get_plaid_products(),
            )

        return jsonify({"link_token": token})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/exchange")
def api_exchange():
    body = request.get_json(force=True)
    public_token = body.get("public_token")

    if not public_token:
        return jsonify({"ok": False, "error": "missing public_token"}), 400

    try:
        access_token, item_id = exchange_public_token(_plaid_client, public_token)
        institution_name = get_institution_name(_plaid_client, access_token)

        migrate(_settings.db_path)
        conn = get_connection(_settings.db_path)
        try:
            insert_item(conn, item_id, access_token, institution_name)
            conn.commit()
        finally:
            conn.close()

        # Shut down the server half a second after responding so the browser
        # receives the success response before the process exits.
        threading.Timer(0.5, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        return jsonify({"ok": True, "institution": institution_name, "item_id": item_id})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("LINK_SERVER_PORT", _settings.link_server_port))
    print(f"\n  Open http://localhost:{port} in your browser\n")
    app.run(host="127.0.0.1", port=port, debug=False)
