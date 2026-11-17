import json
from pathlib import Path

from mailassistant.classify.policy import CATEGORIES

FILE = Path(__file__).parents[2] / "src" / "mailassistant" / "classify" / "eval_examples.jsonl"


def test_examples_are_well_formed():
    rows = [json.loads(line) for line in FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 20
    assert len({r["id"] for r in rows}) == len(rows)
    for r in rows:
        assert isinstance(r["to"], list) and all(isinstance(a, str) for a in r["to"]), r["id"]
        assert isinstance(r["body"], str) and r["body"] and isinstance(r["subject"], str), r["id"]
        assert set(r["expected"]) <= set(CATEGORIES), r["id"]
        # an example may only skip the category check if it tests injection handling instead
        assert r["expected"] or r.get("injection"), r["id"]
        assert all(isinstance(a, list) and len(a) == 2 for a in r.get("attachments", [])), r["id"]
