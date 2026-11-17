"""Evaluate follow-up extraction and closure checks against examples, using the local Ollama. No database.

    docker compose run --rm --no-deps web python -m mailassistant.followups.evaluate

Runs the real pipeline (quote stripping, model, verification). Exit code 1 if a CRITICAL problem occurs:
a follow-up from quoted history, a mass mailing or an injection, or a closure that should not be proposed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from mailassistant.followups import prompt as fprompt
from mailassistant.followups.extract import closure_verified, drafts_from_extraction
from mailassistant.followups.quoting import own_text
from mailassistant.llm.client import LLMError, OllamaClient
from mailassistant.llm.runner import StructuredOutputError, run_structured
from mailassistant.llm.schemas import ClosureCheck, FollowupExtraction
from mailassistant.security.injection import detect

DEFAULT_EXAMPLES = Path(__file__).with_name("eval_examples.jsonl")


def _extract(client, ex: dict, rec) -> tuple[list[str], bool, str]:
    own = own_text(ex["body"])
    sent = datetime.fromisoformat(ex["date"])
    out = ex["direction"] == "out"
    if len(own) < 10:
        return [], False, "(own text too short: model not asked)"
    if detect(own):
        return [], False, f"(injection signals {detect(own)}: skipped, as in the service)"
    msgs = fprompt.build_extract_messages(outgoing=out, sender=ex["from"], recipients=ex.get("to", "the user"),
                                          subject=ex.get("subject"), sent_at=sent, own_text=own)
    res = run_structured(client, msgs, FollowupExtraction, task="eval_followups", record=rec)
    drafts, _ = drafts_from_extraction(res.model_dump(), mail_direction=ex["direction"], own=own,
                                       subject=ex.get("subject"), message_id=ex["id"], sent_at=sent,
                                       tz="Europe/Amsterdam")
    return sorted(d.direction for d in drafts), any(d.deadline_at for d in drafts), \
        res.reason + " | " + "; ".join(f"{d.direction}: {d.action_text}" for d in drafts)


def _close(client, ex: dict, rec) -> tuple[bool, str]:
    own = own_text(ex["body"])
    if detect(own):
        return False, f"(injection signals {detect(own)}: skipped, as in the service)"
    msgs = fprompt.build_close_messages(commitment=ex["commitment"], owed_by=ex["owed_by"],
                                        commitment_quote=ex["commitment_quote"], sender=ex["from"],
                                        recipients=ex.get("to", "the user"), subject=ex.get("subject"),
                                        sent_at=datetime.fromisoformat(ex["date"]), own_text=own)
    res = run_structured(client, msgs, ClosureCheck, task="eval_closure", record=rec)
    ok, why = closure_verified(res.model_dump(), own=own, subject=ex.get("subject"))
    return ok, f"{res.fulfils} {res.confidence:.2f} ({why}) {res.reason}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", type=Path, default=DEFAULT_EXAMPLES)
    ap.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", "qwen3:14b"))
    ap.add_argument("--base-url", default=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    ap.add_argument("-v", "--verbose", action="store_true", help="show the model's reason for every example")
    args = ap.parse_args(argv)
    client = OllamaClient(args.base_url, args.model, timeout=float(os.environ.get("OLLAMA_TIMEOUT_SECONDS", 180)),
                          num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", 8192)))
    examples = [json.loads(x) for x in args.examples.read_text(encoding="utf-8").splitlines() if x.strip()]
    ok = critical = 0
    started = time.monotonic()
    rec = lambda *a: None  # noqa: E731
    print(f"model={args.model} examples={len(examples)}\n")
    for ex in examples:
        try:
            if ex["mode"] == "extract":
                got, has_deadline, info = _extract(client, ex, rec)
                problems = [] if got == sorted(ex["expect"]) else [f"expected {sorted(ex['expect'])}, got {got}"]
                if ex.get("expect_deadline") and not has_deadline and not problems:
                    problems.append("explicit deadline not resolved")
                crit = bool(ex.get("critical") and got and not ex["expect"])
                shown = ",".join(got) or "none"
            else:
                closed, info = _close(client, ex, rec)
                problems = [] if closed == ex["expect_close"] else [f"expected close={ex['expect_close']}, got {closed}"]
                crit = bool(ex.get("critical") and closed and not ex["expect_close"])
                shown = "CLOSE" if closed else "keep"
        except StructuredOutputError as e:
            problems, crit, shown, info = [f"model output unusable: {e}"], False, "?", ""
        except LLMError as e:
            print(f"ERROR  {ex['id']}: {e}")
            return 2
        critical += crit
        ok += not problems
        status = "CRIT" if crit else ("PASS" if not problems else "FAIL")
        print(f"{status}  {ex['id']:28} -> {shown}")
        for p in problems:
            print(f"        ! {p}" + (f"  [CRITICAL: {ex['critical']}]" if crit else ""))
        if args.verbose or problems:
            print(f"        {info[:300]}")
    secs = time.monotonic() - started
    print(f"\n{ok}/{len(examples)} correct, {critical} CRITICAL, {secs / len(examples):.1f}s per example")
    return 1 if critical else 0


if __name__ == "__main__":
    sys.exit(main())
