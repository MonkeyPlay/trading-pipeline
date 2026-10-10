# research/tabpfn_arm.py
"""
ml_study_v1's tabular foundation-model comparator (research/ml_study.py, family TPF): TabPFN v2 on the
compact NQ features, one configuration (the package defaults), fitted on each outer fold's labelled
training rows and predicting its test rows. Runs in .venv-research ONLY - torch and tabpfn are research
dependencies, never installed in the production .venv - and needs nothing of this project but numpy:

    TABPFN_MODEL_CACHE_DIR=data/research/tabpfn_models .venv-research/bin/python -I research/tabpfn_arm.py \
        data/research/ml_study_v1 preopen rth_h15_cutoff rth_h15_delayed

(scripts/ml_study.py tabpfn prints the exact command.) Weights: TabPFN v2 classifier
(tabpfn-v2-classifier-finetuned-zk73skhh.ckpt, Prior Labs License - Apache 2.0 with an attribution
requirement: "Built with PriorLabs-TabPFN"); the later versions' weights are non-commercial and are not
used. Inference is local on the CPU; the weights are downloaded once and checked by sha256 here.
"""

import csv
import hashlib
import json
import os
import sys
import time

import numpy as np

WEIGHTS = "tabpfn-v2-classifier-finetuned-zk73skhh.ckpt"
WEIGHTS_SHA256 = "cf8c519c01eaf1613ee91239006d57b1c806ff5f23ac1aeb1315ba1015210e49"
CLASSES = ("bearish", "bullish", "neutral_band")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(work_dir, name):
    import torch
    import tabpfn
    from tabpfn import TabPFNClassifier
    from tabpfn.constants import ModelVersion
    cache = os.environ.get("TABPFN_MODEL_CACHE_DIR")
    if not cache or _sha(os.path.join(cache, WEIGHTS)) != WEIGHTS_SHA256:
        raise SystemExit(f"the pinned TabPFN v2 weights ({WEIGHTS}, sha256 {WEIGHTS_SHA256[:12]}) are not in "
                         f"TABPFN_MODEL_CACHE_DIR={cache}")
    d = np.load(os.path.join(work_dir, f"tabpfn_input_{name}.npz"), allow_pickle=True)
    X, y, row_session = d["X"].astype(np.float32), d["y"].astype(int), d["row_session"].astype(int)
    out_rows, log = [], []
    for k, (train_s, test_s) in enumerate(zip(d["folds_train"], d["folds_test"])):
        tr = np.flatnonzero(np.isin(row_session, train_s) & (y >= 0))
        te = np.flatnonzero(np.isin(row_session, test_s))
        t0 = time.time()
        clf = TabPFNClassifier.create_default_for_version(ModelVersion.V2, device="cpu", random_state=0)
        clf.fit(X[tr], y[tr])
        P = clf.predict_proba(X[te])
        full = np.zeros((len(te), len(CLASSES)))
        for j, c in enumerate(clf.classes_):
            full[:, int(c)] = P[:, j]
        for i, r in enumerate(te):
            out_rows.append([name, int(r), k] + [f"{x:.10g}" for x in full[i]])
        log.append({"fold": k, "train_rows": int(len(tr)), "test_rows": int(len(te)),
                    "seconds": round(time.time() - t0, 1)})
        print(f"{name} fold {k}: {len(tr)} training rows, {len(te)} test rows, {time.time() - t0:.1f} s", flush=True)
    path = os.path.join(work_dir, f"predictions_tabpfn_{name}.csv")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["problem_input", "row", "fold"] + [f"p_{c}" for c in CLASSES])
        w.writerows(out_rows)
    meta = {"name": name, "folds": log, "sha256": _sha(path), "weights": WEIGHTS, "weights_sha256": WEIGHTS_SHA256,
            "tabpfn": getattr(tabpfn, "__version__", "9.1.0"), "torch": torch.__version__,
            "threads": torch.get_num_threads(), "params": {k: str(v) for k, v in clf.get_params().items()
                                                           if k != "model_path"},
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    with open(os.path.join(work_dir, f"predictions_tabpfn_{name}.json"), "w") as f:
        json.dump(meta, f, indent=1)
    return meta


if __name__ == "__main__":
    work = sys.argv[1]
    for n in sys.argv[2:]:
        run(work, n)
