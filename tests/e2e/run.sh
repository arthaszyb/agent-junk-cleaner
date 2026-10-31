#!/bin/sh
# Browser E2E for the portal (dev only). Needs Node, Playwright 1.61.1 and a Chromium build; it exits 0 with
# "SKIPPED" when one is missing. Playwright is never added to the repo: it is found in the npx cache, or fetched
# there with `npx -y -p playwright@1.61.1`.
#   E2E_OUT=dir   where the screenshots go (default: a new temp dir)
#   E2E_PYTHON=   the interpreter that runs scripts/dev_portal_fixture.py (default: python3)
set -eu
here=$(cd "$(dirname "$0")" && pwd)

if ! command -v node >/dev/null 2>&1; then
  echo "SKIPPED: node is not installed"
  exit 0
fi
if [ -z "${PLAYWRIGHT_NODE_MODULES:-}" ] && command -v npx >/dev/null 2>&1; then
  # puts playwright into the npx cache if it is not there yet and prints that cache's node_modules folder
  PLAYWRIGHT_NODE_MODULES=$(npx -y -p playwright@1.61.1 sh -c 'cd "$(dirname "$(command -v playwright)")/.." && pwd' 2>/dev/null || true)
  export PLAYWRIGHT_NODE_MODULES
fi
exec node "$here/portal_e2e.mjs"
