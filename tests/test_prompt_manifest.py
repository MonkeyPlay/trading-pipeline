# tests/test_prompt_manifest.py
"""The source prompts in prompts/source/ are the files prompts/manifest.json hashed."""

import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_source_prompts_match_their_manifest_hashes():
    manifest = json.load(open(os.path.join(ROOT, "prompts", "manifest.json")))
    assert {s["id"] for s in manifest["sources"]} == {"P1", "P2"}
    for s in manifest["sources"]:
        data = open(os.path.join(ROOT, s["path"]), "rb").read()
        assert hashlib.sha256(data).hexdigest() == s["sha256"], f"{s['path']} changed since it was hashed"
        assert len(data) == s["bytes"]
