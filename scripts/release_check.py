#!/usr/bin/env python
# scripts/release_check.py
"""
The checks scripts/deploy.sh runs around a deployment (and anyone can run by hand). Read-only; each prints what
it found and exits non-zero when the deployment must not go ahead:

    .venv/bin/python scripts/release_check.py writers   [--prod DIR]    # nothing that writes the database runs
    .venv/bin/python scripts/release_check.py artifacts [--db URL]      # the ML artifacts are the registered ones
    .venv/bin/python scripts/release_check.py schema    [--db URL]      # the database is not newer than the code

  writers     Auto mode's lock (data/auto_mode.lock) is not held; no dashboard, collector, journal, fan or study
              process runs from the production checkout; no other database connection is active or idle in a
              transaction (idle connections are listed, not refused)
  artifacts   every ML model the code knows (contracts/nq_ml.ALGORITHMS) and every seven-target bundle
              (contracts/nq_ml_bundle.VERSIONS): its file hashes to its manifest's sha256, which equals its registered
              definition's; every ML definition already registered (features, schemas, market labels, algorithms)
              has the code's definition hash; the forward evaluation in force (contracts/nq_ml.FORWARD) pins exactly
              these artifacts and this feature version
  schema      the database's schema version against the code's latest migration: newer refuses (older code);
              older lists the pending migrations a deployment would apply
"""

import argparse
import fcntl
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# a process started from the production checkout whose command line names one of these writes the database
WRITER_MARKERS = ("dashboard.app", "dashboard/app.py", "collector", "scripts/nq_journal.py", "scripts/fan.py",
                  "scripts/ml_study.py", "scripts/run_pipeline.sh", "database.migrations", "database/migrations.py")


def _cmdline(pid: str) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return f.read().replace(b"\0", b" ").decode(errors="replace").strip()
    except OSError:
        return ""


def _ancestors() -> set:
    """This process and its parents (the deployment itself is not a writer)."""
    out, pid = set(), os.getpid()
    while pid > 1 and pid not in out:
        out.add(pid)
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return out


def writer_processes(prod: str):
    """``[(pid, cmdline)]`` of the processes running from ``prod`` that write the database."""
    prod = os.path.realpath(prod)
    mine = _ancestors()
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in mine:
            continue
        try:
            cwd = os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            continue
        cmd = _cmdline(pid)
        if (cwd == prod or prod in cmd) and any(m in cmd for m in WRITER_MARKERS) and "release_check" not in cmd:
            out.append((int(pid), cmd))
    return sorted(out)


def auto_lock_held(prod: str) -> bool:
    path = os.path.join(prod, "data", "auto_mode.lock")
    if not os.path.exists(path):
        return False
    with open(path, "a+") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def check_writers(prod: str, db: str) -> int:
    problems = []
    if auto_lock_held(prod):
        holder = open(os.path.join(prod, "data", "auto_mode.lock")).read().strip()
        problems.append(f"Auto mode is on ({holder}): switch it off and stop the dashboard")
    for pid, cmd in writer_processes(prod):
        problems.append(f"pid {pid} runs from {prod}: {cmd[:120]}")
    import psycopg
    with psycopg.connect(db, autocommit=True) as c:
        rows = c.execute("SELECT pid, coalesce(application_name, ''), state, coalesce(client_addr::text, 'local'), "
                         "left(coalesce(query, ''), 80) FROM pg_stat_activity WHERE datname = current_database() "
                         "AND pid <> pg_backend_pid() AND backend_type = 'client backend' ORDER BY pid;").fetchall()
    for pid, app, state, addr, query in rows:
        line = f"database connection {pid} ({app or 'no name'}, {addr}): {state}: {query}"
        if state in ("active", "idle in transaction", "idle in transaction (aborted)"):
            problems.append(line)
        else:
            print(f"note: {line} - idle, not refused")
    for p in problems:
        print(f"WRITER: {p}")
    print("writers: none running" if not problems else f"writers: {len(problems)} - stop them before deploying")
    return 0 if not problems else 1


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_artifacts(db: str) -> int:
    import psycopg
    from contracts import nq_ml as ml
    from forecaster import ml_model as mm
    problems, installed = [], {}
    with psycopg.connect(db, autocommit=True) as c:
        def registered(version):
            row = c.execute("SELECT definition FROM journal.definition_versions WHERE version = %s;",
                            (version,)).fetchone()
            return None if row is None else (row[0] if isinstance(row[0], dict) else json.loads(row[0]))
        for version in ml.ALGORITHMS:
            man = mm.manifest(version)
            reg = registered(version)
            path = os.path.join(mm.artifact_dir(version), "model.joblib")
            if man is None or not os.path.exists(path):
                if reg is not None:
                    problems.append(f"{version}: registered but its artifact is not installed in {mm.MODELS_DIR}")
                else:
                    print(f"{version}: no artifact, not registered - nothing to check")
                continue
            sha = _sha(path)
            installed[version] = sha
            if sha != man["sha256"]:
                problems.append(f"{version}: model.joblib hashes to {sha[:12]}, its manifest says {man['sha256'][:12]}")
            elif reg is not None and (reg.get("artifact") or {}).get("sha256") != sha:
                problems.append(f"{version}: installed {sha[:12]} is not the registered "
                                f"{((reg.get('artifact') or {}).get('sha256') or '?')[:12]}")
            else:
                print(f"{version}: {sha[:12]} = manifest" + (" = registered" if reg is not None else " (not registered)"))
        from contracts import nq_ml_bundle as mb
        from forecaster import ml_bundle as mbun
        for version in mb.VERSIONS.values():
            man = mbun.manifest(version)
            reg = registered(version)
            path = os.path.join(mbun.artifact_dir(version), "bundle.joblib")
            if man is None or not os.path.exists(path):
                if reg is not None:              # shadow: nothing depends on it, so a removal is noted, not refused
                    print(f"{version}: registered but not installed in {mm.MODELS_DIR} - a shadow bundle, no "
                          "forecast or registered evaluation depends on it (stopped by removing its artifact?)")
                else:
                    print(f"{version}: no artifact, not registered - nothing to check")
                continue
            sha = _sha(path)
            if sha != man["sha256"]:
                problems.append(f"{version}: bundle.joblib hashes to {sha[:12]}, its manifest says {man['sha256'][:12]}")
            elif reg is not None and (reg.get("artifact") or {}).get("sha256") != sha:
                problems.append(f"{version}: installed {sha[:12]} is not the registered "
                                f"{((reg.get('artifact') or {}).get('sha256') or '?')[:12]}")
            else:
                print(f"{version}: {sha[:12]} = manifest" + (" = registered" if reg is not None else " (not registered)"))
        for rec in mm.records() + mbun.records():         # a registered definition never changes under its name
            row = c.execute("SELECT definition_hash FROM journal.definition_versions WHERE version = %s;",
                            (rec["version"],)).fetchone()
            if row is not None and row[0] != rec["definition_hash"]:
                problems.append(f"{rec['version']}: the code's definition hashes to {rec['definition_hash'][:12]}, the "
                                f"registered one to {row[0][:12]} - a changed definition needs a new name")
        forward = registered(ml.FORWARD["name"])
        if forward is not None:
            for arm, pin in (forward.get("pins") or {}).items():
                version = ml.ARMS.get(arm)
                if installed.get(version) != pin.get("sha256"):
                    problems.append(f"{ml.FORWARD['name']} pins {arm} ({version}) to {pin.get('sha256', '?')[:12]}, "
                                    f"installed {(installed.get(version) or 'none')[:12]}")
                if pin.get("feature_version") != ml.FEATURE_VERSION:
                    problems.append(f"{ml.FORWARD['name']} pins feature version {pin.get('feature_version')}, the "
                                    f"code computes {ml.FEATURE_VERSION}")
            if not problems:
                print(f"{ml.FORWARD['name']}: its pins match the installed artifacts and {ml.FEATURE_VERSION}")
    for p in problems:
        print(f"ARTIFACT: {p}")
    print("artifacts: verified" if not problems else f"artifacts: {len(problems)} problem(s)")
    return 0 if not problems else 1


def check_schema(db: str) -> int:
    import psycopg
    from database.migrations import discover_migrations
    files = discover_migrations()
    latest = files[-1][0] if files else 0
    with psycopg.connect(db, autocommit=True) as c:
        row = c.execute("SELECT coalesce(max(version), 0) FROM schema_migrations;").fetchone()
    current = row[0]
    if current > latest:
        print(f"schema: the database is at v{current}, newer than this code's v{latest} - refused")
        return 1
    pending = [os.path.basename(p) for v, p in files if v > current]
    print(f"schema: database v{current}, code v{latest}" + (f"; pending: {', '.join(pending)}" if pending else
                                                            "; nothing pending"))
    return 0


def main(argv=None) -> int:
    from config import Config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("check", choices=["writers", "artifacts", "schema"])
    ap.add_argument("--prod", default=ROOT, help="the production checkout (default: this one)")
    ap.add_argument("--db", default=Config.DATABASE_URL)
    args = ap.parse_args(argv)
    if args.check == "writers":
        return check_writers(args.prod, args.db)
    if args.check == "artifacts":
        return check_artifacts(args.db)
    return check_schema(args.db)


if __name__ == "__main__":
    sys.exit(main())
