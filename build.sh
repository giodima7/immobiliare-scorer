#!/usr/bin/env bash
# build.sh — Cloudflare Pages build step.
#
# Substitutes the %% placeholders in dashboard/index.html with the live
# Supabase credentials from environment variables. Idempotent — safe to
# re-run because the second run finds no placeholders to replace.
#
# Cloudflare Pages settings → Build configuration:
#   Build command:        bash build.sh
#   Build output dir:     dashboard
#   Environment vars:     SUPABASE_URL, SUPABASE_ANON_KEY
#
# If either variable is missing the script exits 0 — the dashboard then
# falls back to fetching the static JSON snapshot, so the deploy stays
# working even if Supabase isn't configured yet.

set -euo pipefail

INDEX="dashboard/index.html"
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
