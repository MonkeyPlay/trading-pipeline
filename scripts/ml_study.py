#!/usr/bin/env python
# scripts/ml_study.py
"""
ml_study_v1 - the bounded development study of NQ direction (research/ml_study.py, report
docs/reports/ml_study.md). Reads the database read-only; writes data/research/ml_study_v1/ and
docs/reports/ml_study_v1/. Run the stages in order (``all`` runs every one):

    python scripts/ml_study.py predict-preopen    # pre-open task: frozen arms, candidate ladder, diagnostics
    python scripts/ml_study.py predict-rth        # RTH rolling targets: every horizon and origin
    python scripts/ml_study.py analogues          # B_rth / B_rth_recent at the RTH test sessions
    python scripts/ml_study.py tabpfn             # TabPFN v2 in .venv-research (never the production .venv)
    python scripts/ml_study.py controls           # shuffled labels, synthetic signal, future-bar invariance
    python scripts/ml_study.py score              # checks the stored predictions, then reads the outcomes

Every stage's outputs are recorded with their sha256 in docs/reports/ml_study_v1/manifest.json; the score
stage refuses predictions whose files changed after they were recorded.
"""

import argparse
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH_PY = os.path.join(ROOT, ".venv-research", "bin", "python")
TABPFN_CACHE = os.path.join(ROOT, "data", "research", "tabpfn_models")
TABPFN_INPUTS = ("preopen", "rth_h15_cutoff", "rth_h15_delayed")


def tabpfn_command():
    from research.ml_study_run import OUT
    return [RESEARCH_PY, "-I", os.path.join(ROOT, "research", "tabpfn_arm.py"), OUT, *TABPFN_INPUTS]


def run_tabpfn():
    """TabPFN in its own environment and process, its working directory the study's data directory (so it reads no
    project .env); the weights from the pinned cache."""
    from research.ml_study_run import OUT, _record, _sha
    import json
    if not os.path.exists(RESEARCH_PY):
        raise SystemExit("no .venv-research: python3 -m venv .venv-research && .venv-research/bin/pip install "
                         "-r research/requirements-tabpfn.txt")
    env = {k: v for k, v in os.environ.items() if not k.endswith("_KEY") and k != "DATABASE_URL"}
    # TABPFN_ALLOW_CPU_LARGE_DATASET lifts the package's refusal of more than 1000 training rows on a CPU (a speed
    # guard; the model and its defaults are unchanged) - the RTH folds train on 1400 to 3000 rows
    env.update(TABPFN_MODEL_CACHE_DIR=TABPFN_CACHE, HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1",
               TABPFN_ALLOW_CPU_LARGE_DATASET="1")
    subprocess.run(tabpfn_command(), cwd=OUT, env=env, check=True)
    files = {}
    for name in TABPFN_INPUTS:
        with open(os.path.join(OUT, f"predictions_tabpfn_{name}.json")) as f:
            meta = json.load(f)
        if meta["sha256"] != _sha(os.path.join(OUT, f"predictions_tabpfn_{name}.csv")):
            raise SystemExit(f"predictions_tabpfn_{name}.csv changed after it was written")
        files[f"predictions_tabpfn_{name}.csv (data/research)"] = meta["sha256"]
        files[f"meta_{name}"] = {k: v for k, v in meta.items() if k != "sha256"}
    _record("tabpfn", {"files": files, "environment": "TABPFN_ALLOW_CPU_LARGE_DATASET=1 (CPU size guard lifted), "
                                                     "HF_HUB_OFFLINE=1, no API keys or DATABASE_URL in the process"})


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["predict-preopen", "predict-rth", "analogues", "tabpfn", "controls", "score",
                                      "all"])
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    args = ap.parse_args(argv)
    from research import ml_study_data as dd
    from research import ml_study_run as run
    stages = (["predict-preopen", "predict-rth", "analogues", "tabpfn", "controls", "score"]
              if args.stage == "all" else [args.stage])
    for stage in stages:
        if stage == "tabpfn":
            run_tabpfn()
            continue
        conn = dd.connect()
        try:
            if stage == "predict-preopen":
                print(run.predict_preopen(conn, args.jobs)["files"])
            elif stage == "predict-rth":
                print(run.predict_rth(conn, args.jobs)["files"])
            elif stage == "analogues":
                print(run.analogues(conn, args.jobs)["files"])
            elif stage == "controls":
                run.controls(conn, args.jobs)
            elif stage == "score":
                from research import ml_study_report as rep
                print(rep.score(conn))
        finally:
            conn.close()


if __name__ == "__main__":
    main()
