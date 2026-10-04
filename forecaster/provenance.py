# forecaster/provenance.py
"""The code revision a journal record was produced by (guideline revision 2: inference requests and forecast runs
record it)."""

import functools
import os
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@functools.lru_cache(maxsize=1)
def code_revision() -> str:
    """The git commit of the working tree, with ``+dirty`` when tracked files have uncommitted changes; ``unknown``
    outside a git checkout. Read once per process."""
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True,
                                       stderr=subprocess.DEVNULL).strip()
        dirty = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=_ROOT,
                               stderr=subprocess.DEVNULL).returncode != 0
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return head + ("+dirty" if dirty else "")
