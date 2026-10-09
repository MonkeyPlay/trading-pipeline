#!/usr/bin/env bash
# scripts/deploy.sh
# Deploys one tested revision to production - the main checkout (~/trading_pipeline) on branch main, where the
# dashboard runs (the user's choice, 2026-10-09) - and is the only way the production database is migrated.
#
#   scripts/deploy.sh <revision>            e.g. scripts/deploy.sh 1a2b3c4 (a commit whose tests passed)
#
# 1. refuses unless production (PROD_DIR, default this repository's main checkout) is on branch main with no local
#    changes to tracked files - production runs committed revisions only
# 2. fast-forwards main to <revision>; anything but a fast-forward is refused, so nothing is rewritten
# 3. applies that revision's pending migrations to DATABASE_URL (from its .env), then checks the schema is the one
#    the revision expects
# 4. appends the deployment to logs/deployments.log
#
# Then restart the dashboard there (python -m dashboard.app) and switch Auto on: a running dashboard keeps the code
# it started with, while each job it starts runs the files on disk. Deploy outside a session. Routine processes only
# check the schema and stop on a mismatch, so a migration file can never change the database by itself.

set -euo pipefail

REV="${1:?usage: scripts/deploy.sh <revision>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="$(dirname "$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)")"
PROD="${PROD_DIR:-$MAIN}"

BRANCH="$(git -C "$PROD" branch --show-current)"
if [ "$BRANCH" != "main" ]; then
    echo "ERROR: $PROD is on '${BRANCH:-a detached HEAD}', not main; production runs main." >&2
    exit 1
fi
if [ -n "$(git -C "$PROD" status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: $PROD has local changes; production runs committed revisions only." >&2
    git -C "$PROD" status --short --untracked-files=no >&2
    exit 1
fi
SHA="$(git -C "$PROD" rev-parse --verify "${REV}^{commit}")"
if ! git -C "$PROD" merge-base --is-ancestor HEAD "$SHA"; then
    echo "ERROR: $REV ($SHA) is not a fast-forward of main ($(git -C "$PROD" rev-parse HEAD)); nothing changed." >&2
    exit 1
fi
git -C "$PROD" merge --ff-only --quiet "$SHA"

cd "$PROD"
PY="$PROD/.venv/bin/python"
DB="$("$PY" -c 'from config import Config; print(Config.DATABASE_URL)')"
"$PY" -m database.migrations --db "$DB"
VERSION="$("$PY" -c 'from config import Config; from database.connection import init_database; print(init_database(Config.DATABASE_URL))')"
mkdir -p "$PROD/logs"
echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] deployed $SHA ($REV) to $PROD (main), schema v$VERSION" \
    | tee -a "$PROD/logs/deployments.log"
echo "Restart the dashboard in $PROD (.venv/bin/python -m dashboard.app) and switch Auto on."
