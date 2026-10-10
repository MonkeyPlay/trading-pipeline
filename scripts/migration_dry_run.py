#!/usr/bin/env python
# scripts/migration_dry_run.py
"""
Rehearses the pending migrations on a database in ONE transaction that is rolled back - the check to run before
scripts/deploy.sh applies them for real:

    .venv/bin/python scripts/migration_dry_run.py                  # the database of .env (DATABASE_URL)
    .venv/bin/python scripts/migration_dry_run.py --db postgresql://...

Inside the transaction: every table's rows before, the pending migration files (database/migrations after the
database's current version, their Python hooks included), every table's rows after. Reported per table: rows
removed, added or changed - a row is compared on the columns it had before, so a new column is reported as such,
not as a changed row. Market-data tables are compared by count and an order-independent hash sum. Then ROLLBACK,
and a check that the schema version, the table list, every column, constraint, index, function and trigger, and
every row are what they were. Nothing is committed. A migration that fails is reported (exit 2) and rolled back the
same way. Run it outside a session, with the writers stopped: the migration's locks are held until the rollback.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LARGE = {("public", "bars"), ("public", "collection_runs"), ("public", "session_days"), ("public", "active_contracts")}


def _tables(cur):
    return cur.execute("SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       "WHERE c.relkind IN ('r', 'p') AND n.nspname IN ('public', 'journal') ORDER BY 1, 2").fetchall()


def _columns(cur, schema, name):
    return [r[0] for r in cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = %s "
                                      "AND table_name = %s ORDER BY ordinal_position", (schema, name)).fetchall()]


def fingerprint(cur) -> str:
    """The schema of journal and public: every column, constraint, index, function and trigger - for the check that
    the rollback left nothing behind."""
    import hashlib
    parts = [cur.execute("SELECT table_schema, table_name, column_name, data_type, is_nullable, column_default FROM "
                         "information_schema.columns WHERE table_schema IN ('journal', 'public') ORDER BY 1, 2, 3"
                         ).fetchall(),
             cur.execute("SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                         "connamespace IN ('journal'::regnamespace, 'public'::regnamespace) ORDER BY 1, 2").fetchall(),
             cur.execute("SELECT schemaname, tablename, indexname, indexdef FROM pg_indexes WHERE schemaname IN "
                         "('journal', 'public') ORDER BY 1, 2, 3").fetchall(),
             cur.execute("SELECT n.nspname, p.proname, md5(p.prosrc) FROM pg_proc p JOIN pg_namespace n ON n.oid = "
                         "p.pronamespace WHERE n.nspname IN ('journal', 'public') ORDER BY 1, 2, 3").fetchall(),
             cur.execute("SELECT tgrelid::regclass::text, tgname, tgenabled FROM pg_trigger WHERE NOT tgisinternal "
                         "AND tgrelid::regclass::text NOT LIKE '\\_timescaledb%' ORDER BY 1, 2").fetchall()]
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def state(cur, before=None):
    """Per table its columns and rows (md5 of each row's JSON on the columns it had in ``before``)."""
    out = {}
    for schema, name in _tables(cur):
        key = f"{schema}.{name}"
        cols = _columns(cur, schema, name)
        new = [c for c in cols if before and key in before and c not in before[key]["columns"]]
        row = "to_jsonb(t)" if not new else "(to_jsonb(t) - %s::text[])"
        args = (new,) if new else None
        if (schema, name) in LARGE:
            n, s = cur.execute(f"SELECT count(*), coalesce(sum(hashtextextended({row}::text, 0)::numeric), 0)::text "
                               f"FROM {schema}.{name} t", args).fetchone()
            out[key] = {"columns": cols, "new_columns": new, "large": True, "count": n, "hashsum": s}
        else:
            rows = sorted(r[0] for r in cur.execute(f"SELECT md5({row}::text) FROM {schema}.{name} t", args))
            out[key] = {"columns": cols, "new_columns": new, "large": False, "count": len(rows), "rows": rows}
    return out


def compare(before, after):
    lines, changed = [], 0
    for t in sorted(set(before) | set(after)):
        b, a = before.get(t), after.get(t)
        if b is None:
            lines.append(f"  {t}: new table ({a['count']} rows)")
            continue
        if a is None:
            lines.append(f"  {t}: DROPPED ({b['count']} rows)")
            changed += 1
            continue
        extra = f"; new columns {', '.join(a['new_columns'])}" if a["new_columns"] else ""
        gone = [c for c in b["columns"] if c not in a["columns"]]
        extra += f"; DROPPED columns {', '.join(gone)}" if gone else ""
        if b["large"]:
            same = (b["count"], b["hashsum"]) == (a["count"], a["hashsum"])
            changed += not same
            lines.append(f"  {t}: {a['count']} rows, {'unchanged' if same else 'CHANGED'}{extra}")
            continue
        from collections import Counter
        cb, ca = Counter(b["rows"]), Counter(a["rows"])
        removed, added = sum((cb - ca).values()), sum((ca - cb).values())
        changed += bool(removed or added or gone)
        if removed or added or extra:
            lines.append(f"  {t}: {a['count']} rows - {removed} removed or changed, {added} added{extra}")
    return lines, changed


def main(argv=None):
    import psycopg
    from config import Config
    from database.migrations import POST_HOOKS, PRE_HOOKS, discover_migrations
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=Config.DATABASE_URL)
    args = ap.parse_args(argv)
    with psycopg.connect(args.db, autocommit=False) as conn:
        cur = conn.cursor()
        version = cur.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0]
        pending = [(v, p) for v, p in discover_migrations() if v > version]
        tables_before = _tables(cur)
        if not pending:
            print(f"schema v{version}: nothing pending")
            conn.rollback()
            return 0
        print(f"schema v{version}; rehearsing {', '.join(os.path.basename(p) for _, p in pending)} in one "
              "transaction (rolled back)")
        t0 = time.time()
        schema_before = fingerprint(cur)
        before, failure = state(cur), None
        try:
            for v, path in pending:
                if v in PRE_HOOKS:
                    PRE_HOOKS[v](conn)
                with open(path, encoding="utf-8") as f:
                    cur.execute(f.read())
                if v in POST_HOOKS:
                    POST_HOOKS[v](conn)
            after = state(cur, before)
        except Exception as e:                          # the rehearsal failed: reported, rolled back, checked
            failure = e
        conn.rollback()
        if failure is None:
            lines, changed = compare(before, after)
            print("\n".join(lines) or "  no table changed")
            print(f"{changed} existing table(s) with removed, changed or dropped rows or columns "
                  f"({time.time() - t0:.1f} s); rolled back")
        else:
            print(f"FAILED: {type(failure).__name__}: {str(failure).splitlines()[0] if str(failure) else ''} - rolled "
                  "back; a deployment would fail at the same point")
        cur = conn.cursor()
        again = cur.execute("SELECT coalesce(max(version), 0) FROM schema_migrations").fetchone()[0]
        same = again == version and _tables(cur) == tables_before and fingerprint(cur) == schema_before \
            and state(cur) == before
        conn.rollback()
        print(f"after the rollback: schema v{again}, tables, schema and rows "
              f"{'as before' if same else 'DIFFERENT - investigate before deploying'}")
        if not same:
            return 3
        return 0 if failure is None else 2


if __name__ == "__main__":
    sys.exit(main())
