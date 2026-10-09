# tests/test_prompt_manifest.py
"""The sources in prompts/source/ (P1, P2 and the guideline's Appendix B) are the files prompts/manifest.json hashed;
no LLM prompt is left (removed with the LLM forecasts, 2026-10-09)."""

import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_source_prompts_match_their_manifest_hashes():
    manifest = json.load(open(os.path.join(ROOT, "prompts", "manifest.json")))
    assert {s["id"] for s in manifest["sources"]} == {"P1", "P2", "B"}
    for s in manifest["sources"]:
        data = open(os.path.join(ROOT, s["path"]), "rb").read()
        assert hashlib.sha256(data).hexdigest() == s["sha256"], f"{s['path']} changed since it was hashed"
        assert len(data) == s["bytes"]


def test_no_runtime_prompt_remains():
    manifest = json.load(open(os.path.join(ROOT, "prompts", "manifest.json")))
    assert "runtime" not in manifest
    assert not os.path.exists(os.path.join(ROOT, "prompts", "runtime")) or not os.listdir(
        os.path.join(ROOT, "prompts", "runtime"))
