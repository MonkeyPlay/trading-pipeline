# Reports

| Report | What | Script |
|---|---|---|
| `label_disagreement_v5_vs_nq_v2.md` / `.csv` | The old v5 labels (`nq_labels_v5_candidate`) against the NQ-v2 labels, session by session, with the definition behind every difference (guideline stage 1E) | `scripts/label_disagreement_report.py` |

## Regenerating the disagreement report

Migration 0008 dropped the v5 records, so they come from the backup taken just
before it. Restore its `forecast` schema into a scratch database (one error is
expected: the snapshots' link to `public.contracts`, which the scratch database
does not have):

```bash
C=trading_pipeline-timescaledb-1
docker cp data/backups/trading_pipeline_backup_20261003_201301_before_0008.dump $C:/tmp/before_0008.dump
docker exec $C createdb -U trading tp_scratch_v5_labels
docker exec $C psql -U trading -d tp_scratch_v5_labels -c "CREATE SCHEMA forecast;"
docker exec $C pg_restore -U trading -d tp_scratch_v5_labels --no-owner -n forecast /tmp/before_0008.dump
docker exec $C rm /tmp/before_0008.dump
```

Then, reading the scratch database for v5 and the main database for the journal and
the bars (nothing is written to either):

```bash
python scripts/label_disagreement_report.py --old-db postgresql://trading:trading@localhost:5432/tp_scratch_v5_labels
```

It replays v5 with its own code from git (commit f00a0c1), so the history must be
present. Exit status 1 means a disagreement it could not explain, or a recomputed
NQ-v2 label that differs from the recorded one. Drop the scratch database when
done: `docker exec $C dropdb -U trading tp_scratch_v5_labels`.
