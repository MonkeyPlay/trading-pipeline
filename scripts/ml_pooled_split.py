#!/usr/bin/env python
# scripts/ml_pooled_split.py
"""
ml_pooled_split_v1 - the pooled tuning split corrected and the affected development results rerun
(research/ml_pooled_split.py, report docs/reports/ml_pooled_split_v1.md). Reads the database
read-only; writes docs/reports/ml_pooled_split_v1/ and the report; never touches ml_study_v1's files.

    python scripts/ml_pooled_split.py predict     # both splits' refits, the five-session check, the controls
    python scripts/ml_pooled_split.py score       # checks the prediction files, then reads the outcomes
    python scripts/ml_pooled_split.py all

The predict stage records every file's sha256 (and every ml_study_v1 file's); the score stage refuses a
file that changed after it was recorded.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["predict", "score", "all"])
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    ap.add_argument("--no-controls", action="store_true", help="skip the shuffled-label and planted-signal controls")
    args = ap.parse_args(argv)
    from research import ml_pooled_split as study
    from research import ml_study_data as dd
    for stage in (["predict", "score"] if args.stage == "all" else [args.stage]):
        conn = dd.connect()
        try:
            if stage == "predict":
                m = study.predict(conn, args.jobs, with_controls=not args.no_controls)
                print(f"predict: {len(m['files'])} files recorded in {study.REPORT_DIR}/manifest.json")
            else:
                study.score(conn)
        finally:
            conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
