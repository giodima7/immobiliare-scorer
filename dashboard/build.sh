#!/usr/bin/env bash
# build.sh — Cloudflare Pages build step.
#
# Lives inside dashboard/ because Cloudflare Pages' build environment
# scopes file access to the project root that the user configured. With
# the build output dir set to `dashboard`, the working directory at
# build time is `dashboard/`, so build.sh + index.html sit side by side.
#
# Substitutes the %% placeholders in index.html with the live Supabase
# credentials from environment variables. Idempotent — re-running finds
# no placeholders to replace.
#
# Cloudflare Pages settings → Build configuration:
#   Build command:           bash build.sh
#   Build output directory:  /        (build.sh is already inside dashboard/)
#   Root directory:          dashboard
#   Environment variables:   SUPABASE_URL, SUPABASE_ANON_KEY
#
# If either variable is missing the script exits 0 — the dashboard then
# falls back to the static JSON snapshot, so the deploy stays working
# even if Supabase isn't configured yet.

set -euo pipefail

# Resolve INDEX relative to this script's location, so the build works
# regardless of which directory Cloudflare cd's into before invoking it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEX="${SCRIPT_DIR}/index.html"
if [[ ! -f "$INDEX" ]]; then
  echo "build.sh: $INDEX not found — wrong working directory?" >&2
  exit 1
fi

if [[ -z "${SUPABASE_URL:-}" || -z "${SUPABASE_ANON_KEY:-}" ]]; then
  echo "build.sh: SUPABASE_URL or SUPABASE_ANON_KEY not set."
  echo "          Dashboard will use the static JSON fallback path."
  exit 0
fi

# sed -i is GNU-only; Cloudflare Pages runs Linux so this is fine. The
# delimiters use | so URLs with / don't need escaping. Anon key is a JWT
# (no special chars in standard chars set), so | is also safe.
sed -i "s|%%SUPABASE_URL%%|${SUPABASE_URL}|g"      "$INDEX"
sed -i "s|%%SUPABASE_ANON_KEY%%|${SUPABASE_ANON_KEY}|g" "$INDEX"

echo "build.sh: Supabase credentials injected into $INDEX"
