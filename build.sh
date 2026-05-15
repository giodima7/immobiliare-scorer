#!/usr/bin/env bash
# build.sh — repo-root passthrough for Cloudflare Pages.
#
# Cloudflare's build command runs from the repo root regardless of how
# the project's Root directory is configured. The real script lives in
# dashboard/build.sh (alongside the index.html it patches); this wrapper
# just forwards every env var to it.
#
# Cloudflare Pages settings → Build configuration:
#   Build command:           bash build.sh
#   Build output directory:  dashboard
#   Environment variables:   SUPABASE_URL, SUPABASE_ANON_KEY

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${HERE}/dashboard/build.sh" "$@"
