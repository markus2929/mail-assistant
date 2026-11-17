"""Heuristic detection of prompt-injection attempts in untrusted email text.

This is only a SIGNAL (shown in the UI, blocks automation for that email). The real defences are
structural: the model has no tools, its output is schema-validated, and deterministic code decides
every action from a fixed allowlist.
"""
from __future__ import annotations

import re

_F = re.IGNORECASE | re.DOTALL
_PATTERNS: dict[str, re.Pattern] = {
    "ignore_previous_instructions": re.compile(
        r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|any|your)\b"
        r".{0,40}\b(instructions?|prompts?|rules?|guidelines|context)\b", _F),
    "ignore_previous_instructions_nl": re.compile(
        r"\b(negeer|vergeet|sla over|omzeil)\b.{0,40}\b(vorige|eerdere|bovenstaande|alle|je|uw)\b"
        r".{0,40}\b(instructies|opdrachten|regels|richtlijnen)\b", _F),
    "role_marker": re.compile(r"^\s*(system|assistant|developer)\s*:", re.IGNORECASE | re.MULTILINE),
    "chat_template_tokens": re.compile(r"<\|[a-z_]+\|>|\[/?INST\]|<<\s*/?SYS\s*>>", _F),
    "new_role_or_instructions": re.compile(
        r"\byou are now\b|\bact as (an? )?(ai|assistant|system|developer)\b|\bnew instructions?\s*:", _F),
    "exfiltration_request": re.compile(
        r"\b(forward|send|email|mail|stuur|doorsturen|verstuur)\b.{0,60}\b(this mailbox|the mailbox|"
        r"your mailbox|your inbox|the inbox|deze mailbox|mijn mailbox|passwords?|credentials|"
        r"api[ -]?keys?|wachtwoorden)\b", _F),
    "addressed_to_ai": re.compile(
        r"\b(attention|note to|message for|attn)\b.{0,15}\b(ai|assistant|llm|language model|chatbot)\b", _F),
    "delimiter_spoofing": re.compile(r"</?\s*email\s*>", _F),
}


def detect(text: str) -> list[str]:
    """Names of the injection heuristics that match, sorted."""
    return sorted(name for name, rx in _PATTERNS.items() if rx.search(text or ""))
