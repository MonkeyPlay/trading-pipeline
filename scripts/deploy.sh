#!/usr/bin/env bash
# scripts/deploy.sh
# Deploys one tested revision to production - the main checkout (~/trading_pipeline) on branch main, where the
# dashboard runs (the user's choice, 2026-10-09) - and is the only way the production database is migrated.
#
#   scripts/deploy.sh <revision>            e.g. scripts/deploy.sh 1a2b3c4 (a commit whose tests passed)
#
# 1. refuses unless production (PROD_DIR, default this repository's main checkout) is on branch main with no local
#    changes to tracked files - production runs committed revisions only
# 2. refuses while anything writes the database: Auto mode's lock held, a dashboard, collector, journal, fan or
#    study process running from the production checkout, another connection active or idle in a transaction
#    (scripts/release_check.py writers) - stop the dashboard first, so no job of it is in flight
# 3. fast-forwards main to <revision>; anything but a fast-forward is refused, so nothing is rewritten or reset.
#    Nothing is linked or copied: the runtime files (.env, data/, logs/, .venv) stay where they are
# 4. checks, before the schema changes: the database is not newer than the code, and the installed ML artifacts
#    hash to their manifests and registered definitions and are the ones the forward evaluation pins
# 5. applies that revision's pending migrations to DATABASE_URL (from its .env), then checks the schema is the one
#    the revision expects and the artifacts again
# 6. appends the outcome to logs/deployments.log - a failure after the fast-forward is logged as one, saying where
#    main and the schema were left
#
# It never restarts anything: afterwards start the dashboard there (.venv/bin/python -m dashboard.app) and switch
# Auto on - a running dashboard keeps the code it started with, while each job it starts runs the files on disk.
# Deploy outside a session. Routine processes only check the schema and stop on a mismatch, so a migration file can
# never change the database by itself. Back up and rehearse first (docs/reports/deployment_plan_2026-10-10.md).

set -euo pipefail

REV="${1:?usage: scripts/deploy.sh <revision>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MAIN="$(dirname "$(git -C "$HERE" rev-parse --path-format=absolute --git-common-dir)")"
PROD="${PROD_DIR:-$MAIN}"
PY="$PROD/.venv/bin/python"
LOG="$PROD/logs/deployments.log"
STEP="checks"
START_SHA=""
SHA=""

log() {
    mkdir -p "$PROD/logs"
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG"
}

failed() {
    local code=$?
    if [ "$STEP" != "checks" ] && [ "$STEP" != "done" ]; then
        local schema
        schema="$(cd "$PROD" && "$PY" scripts/release_check.py schema 2>/dev/null | head -n1 || true)"
        log "FAILED at '$STEP' deploying ${SHA:-$REV}: main is at $(git -C "$PROD" rev-parse --short HEAD) (was" \
            "${START_SHA:0:7}); ${schema:-schema unknown}. Nothing was restarted. The dashboard refuses a schema" \
            "that is not its code's - fix forward with a new revision, or see the plan's rollback section." >&2
    fi
    exit "$code"
}
trap failed ERR

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
START_SHA="$(git -C "$PROD" rev-parse HEAD)"
if ! git -C "$PROD" merge-base --is-ancestor HEAD "$SHA"; then
    echo "ERROR: $REV ($SHA) is not a fast-forward of main ($START_SHA); nothing changed." >&2
    exit 1
fi
DB="$(cd "$PROD" && "$PY" -c 'from config import Config; print(Config.DATABASE_URL)')"
TARGET="$(cd "$PROD" && "$PY" -c 'from database.connection import describe_dsn; from config import Config; print(describe_dsn(Config.DATABASE_URL))')"
echo "Deploying $SHA to $PROD (main), database $TARGET"

if ! "$PY" "$HERE/scripts/release_check.py" writers --prod "$PROD" --db "$DB"; then
    echo "ERROR: something writes the database (above); stop it - switch Auto off, stop the dashboard - and run" \
         "this again. Nothing changed." >&2
    exit 1
fi

if [ "$START_SHA" = "$SHA" ]; then
    # already checked out: check before anything changes
    (cd "$PROD" && "$PY" scripts/release_check.py schema --db "$DB" && "$PY" scripts/release_check.py artifacts --db "$DB") \
        || { echo "ERROR: the checks above failed; nothing changed." >&2; exit 1; }
    STEP="migrate"
else
    STEP="fast-forward"
    git -C "$PROD" merge --ff-only --quiet "$SHA"
    STEP="checks of $SHA"
    (cd "$PROD" && "$PY" scripts/release_check.py schema --db "$DB" && "$PY" scripts/release_check.py artifacts --db "$DB")
    STEP="migrate"
fi

cd "$PROD"
"$PY" -m database.migrations --db "$DB"
STEP="schema check"
VERSION="$("$PY" -c 'from config import Config; from database.connection import init_database; print(init_database(Config.DATABASE_URL))')"
STEP="artifact check"
"$PY" scripts/release_check.py artifacts --db "$DB" >/dev/null
STEP="done"
log "deployed $SHA ($REV) to $PROD (main), database $TARGET at schema v$VERSION, artifacts verified - the dashboard" \
    "has not been restarted"
echo "Now start the dashboard in $PROD (.venv/bin/python -m dashboard.app) and switch Auto on; check its pid, cwd" \
     "and 'Auto mode switched on' in logs/pipeline_run.log."
