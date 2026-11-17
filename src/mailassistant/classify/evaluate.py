"""Evaluate the classifier against a file of examples, using the local Ollama. No database.

    docker compose run --rm --no-deps web python -m mailassistant.classify.evaluate

Exit code 1 if any CRITICAL problem occurs (protected mail not protected, an archive suggestion
on protected/injected mail, an injection not flagged).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from mailassistant.classify.core import ClassifyConfig, classify_view
from mailassistant.classify.prompt import EmailView
from mailassistant.classify.scoring import is_critical, score_example
from mailassistant.llm.client import LLMError, OllamaClient

DEFAULT_EXAMPLES = Path(__file__).with_name("eval_examples.jsonl")
MINE = ["me@example.com", "info@example-business.com", "orders@example-business.com"]


def view_from_example(ex: dict) -> EmailView:
    return EmailView(ex.get("from_addr"), ex.get("from_name"), ex.get("to", [MINE[0]]), ex.get("cc", []),
                     ex.get("subject"), datetime.fromisoformat(ex["date"]) if ex.get("date") else None,
                     ex["body"], ex.get("headers", {}), [tuple(a) for a in ex.get("attachments", [])], MINE)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen3:14b"))
    ap.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    ap.add_argument("--context", default=os.environ.get("CLASSIFY_CONTEXT", ""))
    ap.add_argument("-v", "--verbose", action="store_true", help="show the model's reason for every example")
    args = ap.parse_args(argv)

    client = OllamaClient(args.base_url, args.model, timeout=float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", 180)),
                          num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", 8192)))
    cfg = ClassifyConfig(context=args.context)
    examples = [json.loads(line) for line in args.examples.read_text(encoding="utf-8").splitlines() if line.strip()]
    ok = critical = 0
    peak = [0]

    def rec(task, messages, reply, parsed_ok, error):
        peak[0] = max(peak[0], reply.prompt_tokens or 0)

    confusion: Counter = Counter()
    started = time.monotonic()
    print(f"model={args.model} examples={len(examples)}\n")
    for ex in examples:
        try:
            res = classify_view(client, view_from_example(ex), cfg, rec)
        except LLMError as e:
            print(f"ERROR  {ex['id']}: {e}")
            return 2
        d = res.decision
        problems = score_example(ex, d)
        crit = [p for p in problems if is_critical(p)]
        critical += bool(crit)
        ok += not problems
        if problems:
            confusion[((ex.get("expected") or ["(any)"])[0], d.category)] += 1
        status = "PASS" if not problems else ("CRIT" if crit else "FAIL")
        print(f"{status}  {ex['id']:28} -> {d.category:22} conf={d.confidence:.2f} imp={d.importance:6} "
              f"{'PROT ' if d.protected else ''}{'INJ ' if d.injection_flag else ''}"
              f"{'ACTION' if d.actions else ''}")
        for p in problems:
            print(f"        ! {p}")
        if args.verbose:
            print(f"        reason: {d.reasons}")
    if confusion:
        print("\nmismatches (expected -> got):")
        for (want, got), n in confusion.most_common():
            print(f"  {want} -> {got}: {n}")
    secs = time.monotonic() - started
    print(f"\n{ok}/{len(examples)} fully correct, {critical} with CRITICAL problems, "
          f"{secs / len(examples):.1f}s per email, largest prompt {peak[0]} tokens (context {client.num_ctx})")
    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
