import json
import subprocess
import sys

from conftest import REPO

from potency.db import close_pool
from potency.indexing.embeddings import build_index
from potency.sources.importer import import_records
from potency.tenancy import Scope

SCOPE = Scope("public-fda", "casgevy-potency")
SNAPSHOT = "potency-assignment-2026-09-21-v3"

RESTART_SCRIPT = """
import json, sys
from potency.sources.importer import import_records
from potency.indexing.embeddings import build_index
from potency.tenancy import Scope
s = Scope("public-fda", "casgevy-potency")
rep = import_records([sys.argv[1]], expected_scope=s, expected_snapshot=sys.argv[2])
idx = build_index(s, corpus_snapshot=sys.argv[2])
print(json.dumps({"counts": rep["counts"], "embeddings_computed": idx["embeddings_computed"],
                  "index_version": idx["index_version"]}))
"""


def _subset(tmp_path, n=25):
    lines = (REPO / "corpus" / "records.jsonl").read_text(encoding="utf-8").splitlines()[:n]
    p = tmp_path / "subset.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p, lines


def test_unchanged_reimport_after_restart_does_no_work(tmp_path, subprocess_env):
    path, lines = _subset(tmp_path)
    first = import_records([path], expected_scope=SCOPE, expected_snapshot=SNAPSHOT)
    assert first["counts"]["imported"] == len(lines)
    assert first["counts"]["reconciled"]
    idx1 = build_index(SCOPE, corpus_snapshot=SNAPSHOT)
    assert idx1["embeddings_computed"] > 0
    close_pool()

    proc = subprocess.run([sys.executable, "-c", RESTART_SCRIPT, str(path), SNAPSHOT], cwd=REPO,
                          env=subprocess_env, capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, proc.stderr
    second = json.loads(proc.stdout.strip().splitlines()[-1])
    assert second["counts"]["unchanged"] == len(lines)
    assert second["counts"]["imported"] == second["counts"]["updated"] == 0
    assert second["embeddings_computed"] == 0
    assert second["index_version"] == idx1["index_version"]


def test_bad_hash_and_duplicates_are_reported_not_hidden(tmp_path):
    _, lines = _subset(tmp_path, 3)
    bad = json.loads(lines[1])
    bad["text"] = bad["text"] + " tampered"
    p = tmp_path / "bad.jsonl"
    p.write_text("\n".join([lines[0], json.dumps(bad), lines[0], "{not json"]) + "\n", encoding="utf-8")
    rep = import_records([p], expected_scope=SCOPE, expected_snapshot=SNAPSHOT)
    c = rep["counts"]
    assert c["failed"] == 2
    assert c["omitted"] == 1
    assert c["reconciled"]
    reasons = " ".join(e["reason"] for e in rep["errors"])
    assert "sha" in reasons.lower()
