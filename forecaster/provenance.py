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


_OUTPUTS = ("docs/", "data/", "logs/")          # written by runs, not code
_PACKAGES = ("numpy", "pandas", "scikit-learn", "scipy", "psycopg")


def source_snapshot(archive_dir: str = None) -> dict:
    """
    The complete source a result was produced by - every file git tracks or would track (untracked but not ignored),
    except the outputs under docs/, data/ and logs/ - hashed path by path, with the commit, whether the tree differs
    from it, the Python version and the versions of the numerical packages. With ``archive_dir`` the files are also
    archived there as <snapshot>.tar.gz, so a '+dirty' tree stays recoverable.
    """
    import hashlib
    import platform
    import tarfile
    from importlib import metadata

    files = subprocess.check_output(["git", "ls-files", "-co", "--exclude-standard"], cwd=_ROOT, text=True).split()
    files = sorted(f for f in files if not f.startswith(_OUTPUTS) and os.path.isfile(os.path.join(_ROOT, f)))
    h = hashlib.sha256()
    for f in files:
        with open(os.path.join(_ROOT, f), "rb") as fh:
            h.update(f.encode() + b"\0" + hashlib.sha256(fh.read()).digest())
    snapshot = h.hexdigest()[:16]
    revision = code_revision()
    out = {"commit": revision.split("+")[0], "dirty": revision.endswith("+dirty"), "snapshot": snapshot,
           "files": len(files), "python": platform.python_version(),
           "packages": {p: metadata.version(p) for p in _PACKAGES}}
    if archive_dir:
        os.makedirs(archive_dir, exist_ok=True)
        path = os.path.join(archive_dir, f"{snapshot}.tar.gz")
        if not os.path.exists(path):
            with tarfile.open(path, "w:gz") as tar:
                for f in files:
                    tar.add(os.path.join(_ROOT, f), arcname=f)
        out["archive"] = os.path.relpath(path, _ROOT)
    return out
