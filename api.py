#!/usr/bin/env python3
"""
api.py — Flask backend for the Immobiliare Scorer dashboard.
Replaces serve.py.

Usage:
    python3 api.py
    → http://localhost:8000/

Endpoints:
    GET  /           — dashboard HTML
    GET  /listings   — latest.json
    GET  /status     — {"running": bool}
    POST /fetch      — start a fetch; streams stdout as Server-Sent Events
"""
import json as _json
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file, stream_with_context

BASE_DIR      = Path(__file__).parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
SCRIPT        = BASE_DIR / "fetch_listings.py"

app = Flask(__name__)

# Return JSON for all HTTP errors so the browser never sees an HTML error page
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "not found", "path": request.path}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": str(e)}), 500

_fetch_lock    = threading.Lock()
_fetch_running = False

_scanner_lock = threading.Lock()
_scanner_proc = None   # subprocess.Popen for fetch_rentals.py --daemon


@app.route("/api-ping")
def api_ping():
    """Lightweight liveness probe — returns 200 only on the local Flask server.
    Netlify is configured to return 404 for this path.
    The dashboard JS uses this to switch between live-API and static-read-only modes."""
    return jsonify({"ok": True, "mode": "api"})


@app.route("/")
def index():
    return send_file(DASHBOARD_DIR / "index.html")


@app.route("/listings")
def listings():
    path = DASHBOARD_DIR / "latest.json"
    if not path.exists():
        return jsonify([])
    return send_file(path, mimetype="application/json")


@app.route("/status")
def status():
    return jsonify({"running": _fetch_running})


@app.route("/favourites")
def favourites():
    return send_file(DASHBOARD_DIR / "favourites.html")


@app.route("/rentals")
def rentals():
    path = DASHBOARD_DIR / "rentals_latest.json"
    if not path.exists():
        return jsonify([])
    return send_file(path, mimetype="application/json")


@app.route("/start-scanner", methods=["POST"])
def start_scanner():
    global _scanner_proc
    with _scanner_lock:
        if _scanner_proc and _scanner_proc.poll() is None:
            return jsonify({"error": "already running", "pid": _scanner_proc.pid}), 409
        body = request.get_json(silent=True) or {}
        cmd  = [sys.executable, "-u", str(BASE_DIR / "fetch_rentals.py"), "--daemon"]
        areas = body.get("areas")
        if areas:
            # Accept either a list ["Navigli","Brera"] or a legacy comma-string
            if isinstance(areas, list):
                areas = ",".join(str(a) for a in areas if a)
            if areas:
                cmd += ["--areas", areas]
        if body.get("max_rent"):
            cmd += ["--max-rent",  str(int(body["max_rent"]))]
        if body.get("min_rooms"):
            cmd += ["--min-rooms", str(int(body["min_rooms"]))]
        if body.get("pages"):
            cmd += ["--pages",     str(int(body["pages"]))]
        if body.get("email"):
            cmd.append("--email")
        _scanner_proc = subprocess.Popen(cmd, cwd=str(BASE_DIR))
        return jsonify({"started": True, "pid": _scanner_proc.pid})


@app.route("/stop-scanner", methods=["POST"])
def stop_scanner():
    global _scanner_proc
    with _scanner_lock:
        if _scanner_proc and _scanner_proc.poll() is None:
            _scanner_proc.terminate()
            return jsonify({"stopped": True, "pid": _scanner_proc.pid})
    return jsonify({"stopped": False, "reason": "not running"})


@app.route("/scanner-status")
def scanner_status():
    running = _scanner_proc is not None and _scanner_proc.poll() is None
    status  = {"running": running}
    status_path = BASE_DIR / "scanner_status.json"
    if status_path.exists():
        try:
            status.update(_json.loads(status_path.read_text()))
        except Exception:
            pass
    # running field from process is authoritative — override whatever was in file
    status["running"] = running
    return jsonify(status)


@app.route("/email-config", methods=["GET"])
def get_email_config():
    from email_digest import load_config
    cfg = load_config()
    safe = {k: v for k, v in cfg.items() if k != "smtp_pass"}
    safe["smtp_pass"] = "••••••••" if cfg.get("smtp_pass") else ""
    return jsonify(safe)


@app.route("/email-config", methods=["POST"])
def set_email_config():
    from email_digest import load_config, save_config
    body = request.get_json(silent=True) or {}
    cfg  = load_config()
    for key in ("enabled", "smtp_host", "smtp_port", "smtp_user", "to_addrs",
                "digest_hour"):
        if key in body:
            cfg[key] = body[key]
    # Only overwrite password if a real value was sent (not the masked placeholder)
    if body.get("smtp_pass") and body["smtp_pass"] != "••••••••":
        cfg["smtp_pass"] = body["smtp_pass"]
    if "filters" in body:
        cfg["filters"] = {**cfg.get("filters", {}), **body["filters"]}
    save_config(cfg)
    return jsonify({"saved": True})


@app.route("/send-digest-now", methods=["POST"])
def send_digest_now():
    """Immediately send the digest using the current rentals file + email config."""
    from email_digest import load_config, send_digest
    cfg       = load_config()
    rent_path = DASHBOARD_DIR / "rentals_latest.json"
    if not rent_path.exists():
        return jsonify({"error": "No rentals data yet"}), 404
    try:
        listings = _json.loads(rent_path.read_text())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Override enabled flag so test send always fires
    test_cfg           = dict(cfg)
    test_cfg["enabled"] = True
    try:
        send_digest(listings, test_cfg)
        return jsonify({"sent": True, "count": len(listings)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/fetch", methods=["POST"])
def run_fetch():
    global _fetch_running

    with _fetch_lock:
        if _fetch_running:
            return jsonify({"error": "A fetch is already running"}), 409

    body   = request.get_json(silent=True) or {}
    cities = [c for c in body.get("cities", []) if c]
    pages  = body.get("pages")

    # Build subprocess command
    cmd = [sys.executable, "-u", str(SCRIPT)]
    if cities:
        cmd += ["--cities"] + cities
    if pages:
        cmd += ["--pages", str(int(pages))]
    for flag, key in [
        ("--max-price", "max_price"),
        ("--min-price", "min_price"),
        ("--min-sqm",   "min_sqm"),
        ("--max-sqm",   "max_sqm"),
        ("--min-rooms", "min_rooms"),
    ]:
        val = body.get(key)
        if val:
            cmd += [flag, str(int(val))]

    def generate():
        global _fetch_running
        _fetch_running = True
        proc = None
        try:
            yield f"data: $ {' '.join(cmd)}\n\n"
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(BASE_DIR),
            )
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line:
                    yield f"data: {line}\n\n"
            proc.wait()
            rc = proc.returncode
            if rc == 0:
                yield "data: ✓ Fetch complete\n\n"
            else:
                yield f"data: ✗ Failed (exit {rc})\n\n"
            yield "event: done\ndata: {}\n\n"
        except GeneratorExit:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception as exc:
            yield f"data: ✗ {exc}\n\n"
            yield "event: done\ndata: {}\n\n"
        finally:
            _fetch_running = False

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    print(f"\n  Immobiliare Scorer")
    print(f"  Dashboard → http://localhost:8000/")
    print(f"  Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
