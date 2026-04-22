#!/usr/bin/env python3
"""
deploy.py — push dashboard/ to Netlify without running a scan.

Usage:
    python3 deploy.py

Reads netlify_config.json (site_id + token) and uploads every file in
dashboard/ via the Netlify Deploy API. Only changed files are uploaded
(Netlify SHA1-diffs the manifest), so it's fast for HTML/CSS/JS tweaks.
"""

import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR      = Path(__file__).parent
DASHBOARD_DIR = BASE_DIR / "dashboard"
CONFIG_PATH   = BASE_DIR / "netlify_config.json"


def deploy():
    if not CONFIG_PATH.exists():
        print("✗ netlify_config.json not found — add {\"site_id\": \"...\", \"token\": \"...\"}")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    site_id = cfg.get("site_id", "").strip()
    token   = cfg.get("token",   "").strip()
    if not site_id or not token:
        print("✗ site_id or token missing in netlify_config.json")
        sys.exit(1)

    files = {f for f in DASHBOARD_DIR.rglob("*") if f.is_file()}
    if not files:
        print("✗ dashboard/ is empty — nothing to deploy")
        sys.exit(1)

    print(f"  Hashing {len(files)} file(s)…")
    contents: dict[str, bytes] = {}
    hashes:   dict[str, str]   = {}
    for f in files:
        key           = "/" + f.relative_to(DASHBOARD_DIR).as_posix()
        data          = f.read_bytes()
        contents[key] = data
        hashes[key]   = hashlib.sha1(data).hexdigest()

    # Always include netlify.toml from the project root so that redirects and
    # headers defined there are applied to the manual deploy.
    toml_path = BASE_DIR / "netlify.toml"
    if toml_path.exists():
        data = toml_path.read_bytes()
        contents["/netlify.toml"] = data
        hashes["/netlify.toml"]   = hashlib.sha1(data).hexdigest()

    auth = {"Authorization": f"Bearer {token}"}

    # Step 1 — create deploy with file manifest
    req = urllib.request.Request(
        f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
        data=json.dumps({"files": hashes}).encode(),
        headers={**auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            deploy = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"✗ create deploy failed ({e.code}): {e.read()[:300].decode()}")
        sys.exit(1)

    deploy_id = deploy["id"]
    required  = set(deploy.get("required", []))
    print(f"  Deploy {deploy_id} — uploading {len(required)} changed file(s)…")

    # Step 2 — upload only files Netlify doesn't already have cached
    for key, data in contents.items():
        if hashes[key] not in required:
            continue
        req = urllib.request.Request(
            f"https://api.netlify.com/api/v1/deploys/{deploy_id}/files/{key.lstrip('/')}",
            data=data,
            headers={**auth, "Content-Type": "application/octet-stream"},
            method="PUT",
        )
        try:
            urllib.request.urlopen(req, timeout=60)
            print(f"  ↑ {key}")
        except urllib.error.HTTPError as e:
            print(f"✗ upload {key} failed ({e.code})")
            sys.exit(1)

    print(f"✓ {len(files)} file(s) deployed — site is live")


if __name__ == "__main__":
    deploy()
