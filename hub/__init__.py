"""Publish hub: Flask blueprint = the hub's entire HTTP surface.

Why a blueprint instead of a separate service: the PRD draws the hub as
its own box, and this package IS that box — app.py only knows the URL
prefix. Today it runs in the same process (simpler for a POC); the day
it needs to be a real standalone service, this blueprint becomes its own
Flask app and the routes don't change. The seam is the design.

Routes:
  GET  /hub/                      connections page (connect/disconnect UI)
  GET  /hub/connect/<platform>    kick off OAuth (redirect to platform)
  GET  /hub/callback/<platform>   OAuth return leg -> vault
  GET  /hub/integrations          JSON list for the publish picker
  POST /hub/publish               fan out to selected platforms
  GET  /hub/status/<publish_id>   poll per-platform status
"""

import os

from flask import Blueprint, jsonify, redirect, render_template, request

from . import oauth, orchestrator, vault

bp = Blueprint("hub", __name__, url_prefix="/hub")


@bp.get("/")
def connections_page():
    missing = [v for v in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET")
               if not os.environ.get(v)]
    return render_template("hub.html",
                           integrations=vault.list_integrations(),
                           linkedin_missing=missing,
                           youtube_missing=not os.path.exists("client_secret.json"))


@bp.get("/connect/<platform>")
def connect(platform):
    if platform == "linkedin":
        return redirect(oauth.linkedin_auth_url())
    if platform == "youtube":
        return redirect(oauth.youtube_auth_url())
    return jsonify(error="Unknown platform: " + platform), 404


@bp.get("/callback/<platform>")
def callback(platform):
    # CSRF check first: this callback must belong to a flow we started.
    state = request.args.get("state", "")
    if not oauth.check_state(state, platform):
        return "State mismatch — start the connect flow again from /hub/.", 400
    if "error" in request.args:  # user clicked Cancel on the platform page
        return redirect("/hub/")
    code = request.args.get("code", "")
    if platform == "linkedin":
        tok = oauth.linkedin_exchange(code)
    elif platform == "youtube":
        # state travels through to retrieve the PKCE verifier from the first leg
        tok = oauth.youtube_exchange(code, state)
    else:
        return jsonify(error="Unknown platform"), 404
    display = tok.pop("display_name")
    vault.save_integration(platform, display, tok)
    return redirect("/hub/")


@bp.post("/disconnect/<int:integration_id>")
def disconnect(integration_id):
    vault.delete_integration(integration_id)
    return redirect("/hub/")


@bp.get("/integrations")
def integrations():
    return jsonify(vault.list_integrations())


@bp.post("/publish")
def publish():
    data = request.get_json(silent=True) or {}
    path = data.get("path", "")
    targets = data.get("targets", [])
    if not os.path.isfile(path):
        return jsonify(error="Video file not found. Generate one first."), 400
    if not targets:
        return jsonify(error="Pick at least one platform."), 400
    # Captions: same shape /generate already returns, so the frontend just
    # forwards what it has. Per-platform overrides can come later.
    captions = {"default": {
        "title": data.get("title", ""),
        "text": data.get("description", ""),
        "tags": data.get("tags", []),
        "privacy": data.get("privacy", "unlisted"),
    }}
    publish_id = orchestrator.start_publish(path, captions, targets)
    return jsonify(publish_id=publish_id)


@bp.get("/status/<publish_id>")
def status(publish_id):
    return jsonify(orchestrator.get_status(publish_id))
