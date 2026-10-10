#!/usr/bin/env python
# scripts/verify_backup.py
"""
Validates a database backup made by scripts/backup_db.sh and records what it is - and, with --restore, proves it
restores, into a disposable database that is never the production one:

    .venv/bin/python scripts/verify_backup.py data/backups/<file>.dump              # record and validate
    .venv/bin/python scripts/verify_backup.py data/backups/<file>.dump --restore    # and a restore test

Writes <file>.dump.json beside the archive:

  file        size and sha256 (a later copy is checked against them)
  archive     pg_restore --list's header: the database it was dumped from, when, by which pg_dump; its table-data
              entries
  source      when recorded at backup time (--at-backup): the database (host:port/name, no credentials), its schema
              version and every journal and public table's row count, read right after the dump - equal to the
              archive's when no writer ran (backup_db.sh is run with the writers stopped)
  restore     with --restore: restored into RESTORE_DB in the database container with TimescaleDB's procedure
              (timescaledb_pre_restore, pg_restore, timescaledb_post_restore); its schema version and row counts,
              compared with the source's when recorded; the disposable database is dropped afterwards

Exit 0 when the archive is valid (and restored with matching counts); else non-zero. Needs no client tools on the
host: pg_restore and psql run in the database container.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESTORE_DB = "trading_pipeline_restore_check"


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def container() -> str:
    out = subprocess.run(["docker", "ps", "--format", "{{.Names}}", "--filter",
                          f"name={os.getenv('DB_CONTAINER', 'timescaledb')}"], capture_output=True, text=True,
                         check=True).stdout.split()
    if not out:
        raise SystemExit("no running database container")
    return out[0]


def _with_db(url: str, name: str) -> str:
    import psycopg
    info = psycopg.conninfo.conninfo_to_dict(url)
    info["dbname"] = name
    return psycopg.conninfo.make_conninfo(**info)


def archive_list(path: str, box: str) -> dict:
    with open(path, "rb") as f:
        run = subprocess.run(["docker", "exec", "-i", box, "pg_restore", "--list"], stdin=f, capture_output=True)
    if run.returncode != 0:
        raise SystemExit(f"pg_restore --list failed: {run.stderr.decode(errors='replace')[:300]}")
    text = run.stdout.decode(errors="replace")
    head = lambda key: (re.search(rf";\s+{key}:?\s+(.+)", text) or [None, None])[1]     # noqa: E731
    return {"dbname": head("dbname"), "created": head("Archive created at"),
            "dumped_from": head("Dumped from database version"), "dumped_by": head("Dumped by pg_dump version"),
            "toc_entries": len([l for l in text.splitlines() if l and not l.startswith(";")]),
            "table_data_entries": text.count(" TABLE DATA ")}


def counts(url: str) -> dict:
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        version = c.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0]
        tables = c.execute("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                           "WHERE c.relkind IN ('r', 'p') AND n.nspname IN ('journal', 'public') ORDER BY 1, 2"
                           ).fetchall()
        rows = {f"{s}.{t}": c.execute(f'SELECT count(*) FROM "{s}"."{t}"').fetchone()[0] for s, t in tables}
    return {"schema_version": version, "rows": rows}


def restore(path: str, box: str, url: str) -> dict:
    import psycopg
    from database.connection import describe_dsn
    prod_name = psycopg.conninfo.conninfo_to_dict(url).get("dbname")
    if prod_name == RESTORE_DB:
        raise SystemExit("refused: the restore target would be the configured database")
    admin = _with_db(url, "postgres")
    target = _with_db(url, RESTORE_DB)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{RESTORE_DB}"')
        c.execute(f'CREATE DATABASE "{RESTORE_DB}"')
    try:
        with psycopg.connect(target, autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
            c.execute("SELECT timescaledb_pre_restore()")
        with open(path, "rb") as f:
            run = subprocess.run(["docker", "exec", "-i", box, "pg_restore", "--no-owner", "-d", target], stdin=f,
                                 capture_output=True)
        errors = [l for l in run.stderr.decode(errors="replace").splitlines() if "error" in l.lower()]
        with psycopg.connect(target, autocommit=True) as c:
            c.execute("SELECT timescaledb_post_restore()")
        got = counts(target)
        return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "into": describe_dsn(target), "pg_restore_exit": run.returncode, "errors": errors[:20], **got}
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{RESTORE_DB}"')


def main(argv=None) -> int:
    from config import Config
    from database.connection import describe_dsn
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump")
    ap.add_argument("--restore", action="store_true", help="restore into a disposable database and compare")
    ap.add_argument("--at-backup", action="store_true", help="record the source's schema and row counts now "
                                                              "(backup_db.sh, right after the dump)")
    ap.add_argument("--compare-live", action="store_true", help="with --restore and no source counts recorded: "
                                                                 "compare with the live database's counts now")
    ap.add_argument("--db", default=Config.DATABASE_URL)
    args = ap.parse_args(argv)
    path = os.path.abspath(args.dump)
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        print(f"INVALID: {path} is missing or empty")
        return 1
    side = path + ".json"
    rec = json.load(open(side)) if os.path.exists(side) else {}
    box = container()
    sha = _sha(path)
    if rec.get("file", {}).get("sha256") not in (None, sha):
        print(f"INVALID: {path} hashes to {sha[:12]}, recorded {rec['file']['sha256'][:12]} - it changed")
        return 1
    rec["file"] = {"name": os.path.basename(path), "bytes": os.path.getsize(path), "sha256": sha}
    rec["archive"] = archive_list(path, box)
    if args.at_backup:
        rec["source"] = {"recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         "database": describe_dsn(args.db), **counts(args.db)}
    ok = rec["archive"]["table_data_entries"] > 0
    print(f"{rec['file']['name']}: {rec['file']['bytes']:,} bytes, sha256 {sha[:16]}…, dumped from "
          f"{rec['archive']['dbname']} at {rec['archive']['created']}, {rec['archive']['table_data_entries']} "
          "table-data entries" + ("" if ok else " - NO TABLE DATA"))
    if args.restore:
        r = restore(path, box, args.db)
        rec["restore"] = r
        ref = (rec.get("source") or {}).get("rows")
        diff = {t: (ref.get(t), n) for t, n in r["rows"].items() if ref is not None and ref.get(t) != n}
        r["matches_source"] = None if ref is None else not diff and set(ref) == set(r["rows"])
        ok = ok and r["pg_restore_exit"] == 0 and r["matches_source"] is not False
        if ref is None and args.compare_live:              # no counts recorded at backup time: today's, labelled so
            live = counts(args.db)
            r["live_now"] = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "schema_version": live["schema_version"],
                             "tables_only_live": sorted(set(live["rows"]) - set(r["rows"])),
                             "tables_only_restored": sorted(set(r["rows"]) - set(live["rows"])),
                             "differing_counts": {t: [n, live["rows"][t]] for t, n in r["rows"].items()
                                                  if t in live["rows"] and live["rows"][t] != n}}
            print(f"compared with the live database now (schema v{live['schema_version']}, not the source at backup "
                  f"time): tables only live {r['live_now']['tables_only_live']}, only restored "
                  f"{r['live_now']['tables_only_restored']}, differing counts {r['live_now']['differing_counts']}")
        print(f"restored into {r['into']} (dropped again): schema v{r['schema_version']}, {len(r['rows'])} tables, "
              f"{sum(r['rows'].values()):,} rows; pg_restore exit {r['pg_restore_exit']}"
              + (f"; matches the source's counts: {r['matches_source']}" if ref is not None else
                 "; no source counts recorded to compare"))
        for t, (a, b) in list(diff.items())[:10]:
            print(f"  {t}: source {a}, restored {b}")
        for e in r["errors"][:5]:
            print(f"  pg_restore: {e}")
    with open(side, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1, sort_keys=True)
    print(("VALID" if ok else "INVALID") + f": recorded in {os.path.basename(side)}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
