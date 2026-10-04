# tests/test_prompt_manifest.py
"""The sources in prompts/source/ (P1, P2 and the guideline's Appendices A and B) are the files prompts/manifest.json hashed."""

import hashlib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_source_prompts_match_their_manifest_hashes():
    manifest = json.load(open(os.path.join(ROOT, "prompts", "manifest.json")))
    assert {s["id"] for s in manifest["sources"]} == {"P1", "P2", "A", "B"}
    for s in manifest["sources"] + manifest.get("runtime", []):
        data = open(os.path.join(ROOT, s["path"]), "rb").read()
        assert hashlib.sha256(data).hexdigest() == s["sha256"], f"{s['path']} changed since it was hashed"
        assert len(data) == s["bytes"]


def test_the_runtime_prompt_is_the_one_the_claude_protocol_registers():
    from contracts import nq_preopen as pre
    manifest = json.load(open(os.path.join(ROOT, "prompts", "manifest.json")))
    runtime = {r["id"]: r for r in manifest["runtime"]}[pre.LLM_PROTOCOL_VERSION]
    assert pre.llm_record()["definition"]["prompt"]["sha256"] == runtime["sha256"]
