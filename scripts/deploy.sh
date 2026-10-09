#!/usr/bin/env bash
# scripts/deploy.sh
# Deploys one fixed revision for production - the only way the production database is migrated.
#
#   scripts/deploy.sh <revision>            e.g. scripts/deploy.sh fan-freeze, or a commit id
#
# 1. checks the revision out, detached, in the production checkout PROD_DIR (default
#    ~/trading_pipeline_prod, a git worktree of this repository; created on the first deploy,
#    refused when it holds local changes)
# 2. links the runtime state the processes share from the main checkout: .env, .venv, logs/
#    and data/{fan_cache,preview,approvals,models,backups,auto_mode.lock} - one Auto lock, so a
#    development dashboard and the production one can never collect at once
# 3. applies that revision's pending migrations to DATABASE_URL (from its .env), then checks the
#    schema is the one the revision expects
# 4. appends the deployment to logs/deployments.log
#
# Production runs from PROD_DIR: start the dashboard there (and its Auto runs that revision's
# code). Routine processes only check the schema and stop on a mismatch, so a migration file
# created in a development checkout can never change the database by itself.

set -euo pipefail

REV="${1:?usage: scripts/deploy.sh <revision>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# the main checkout holds the shared runtime state, wherever this script is run from (STATE_DIR: elsewhere - tests)
MAIN="$(dirname "$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)")"
STATE="${STATE_DIR:-$MAIN}"
PROD="${PROD_DIR:-$HOME/trading_pipeline_prod}"
SHA="$(git -C "$MAIN" rev-parse --verify "${REV}^{commit}")"

if [ -e "$PROD" ]; then
    if ! git -C "$PROD" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
       || [ "$(git -C "$PROD" rev-parse --path-format=absolute --git-common-dir)" != "$MAIN/.git" ]; then
        echo "ERROR: $PROD exists and is not a checkout of this repository." >&2
        exit 1
    fi
    if [ -n "$(git -C "$PROD" status --porcelain --untracked-files=no)" ]; then
        echo "ERROR: $PROD has local changes; production runs committed revisions only." >&2
        git -C "$PROD" status --short --untracked-files=no >&2
        exit 1
    fi
    git -C "$PROD" checkout --quiet --detach "$SHA"
else
    git -C "$MAIN" worktree add --quiet --detach "$PROD" "$SHA"
fi

mkdir -p "$STATE/logs" "$STATE/data"
touch "$STATE/data/auto_mode.lock"
for path in .env .venv logs data/fan_cache data/preview data/approvals data/models data/backups \
            data/auto_mode.lock; do
    if [ -e "$STATE/$path" ]; then
        if [ -e "$PROD/$path" ] && [ ! -L "$PROD/$path" ]; then
            echo "ERROR: $PROD/$path exists and is not a link to $STATE/$path; move it aside first." >&2
            exit 1
        fi
        ln -sfn "$STATE/$path" "$PROD/$path"
    fi
done

cd "$PROD"
PY="$PROD/.venv/bin/python"
DB="$("$PY" -c 'from config import Config; print(Config.DATABASE_URL)')"
"$PY" -m database.migrations --db "$DB"
VERSION="$("$PY" -c 'from config import Config; from database.connection import init_database; print(init_database(Config.DATABASE_URL))')"
echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] deployed $SHA ($REV) to $PROD, schema v$VERSION" | tee -a "$STATE/logs/deployments.log"
echo "Production runs from $PROD: restart the dashboard there (cd $PROD && .venv/bin/python -m dashboard.app)" \
     "and switch Auto on again."
