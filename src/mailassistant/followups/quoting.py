"""The part of an email its sender actually wrote now: quoted history and forwarded mail removed.

Commitments are only extracted from, and evidence only verified against, this own text - otherwise every
reply would re-create the commitments of the messages it quotes. Standard library only.
"""
from __future__ import annotations

import re

# "Op di 3 nov 2026 om 10:12 schreef Piet <p@x.nl>:" / "On Tuesday, November 3rd, 2026 at 10:12, Piet <p@x> wrote:"
# Both often wrap onto a second (or third) line, so they are matched on a few joined lines.
_ATTRIBUTION = re.compile(r"^\s*(?:op|on|am|le)\s.{3,250}?\b(?:schreef|wrote|schrieb|a écrit)\b\s*:?", re.I | re.S)
_HEADER_FROM = re.compile(r"^\s*\**(?:van|from|von|de)\s*:\**\s*\S", re.I)
_HEADER_NEXT = re.compile(r"^\s*\**(?:verzonden|sent|datum|date|gesendet|envoyé|aan|to|onderwerp|subject)\s*:", re.I)
_SEPARATOR = re.compile(
    r"^\s*[-_=*]{2,}\s*(?:original message|oorspronkelijk bericht|origineel bericht|forwarded message|"
    r"doorgestuurd bericht|weitergeleitete nachricht|begin forwarded message)\b", re.I)
_BEGIN_FWD = re.compile(r"^\s*(?:begin forwarded message|begin doorgestuurd bericht)\s*:", re.I)
_RULE = re.compile(r"^\s*_{10,}\s*$")  # Outlook puts a line of underscores above "Van: ... Verzonden: ..."


def _cut_index(lines: list[str]) -> int | None:
    for i, line in enumerate(lines):
        if _SEPARATOR.match(line) or _BEGIN_FWD.match(line):
            return i
        if _HEADER_FROM.match(line) and any(_HEADER_NEXT.match(n) for n in lines[i + 1:i + 5]):
            return i - 1 if i > 0 and _RULE.match(lines[i - 1]) else i
        if re.match(r"^\s*(?:op|on|am|le)\s", line, re.I) and _ATTRIBUTION.match(" ".join(lines[i:i + 3])):
            return i
    return None


def own_text(body: str | None) -> str:
    """Text above the first quote/forward marker, without '>' lines. Never longer than the input."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    cut = _cut_index(lines)
    if cut is not None:
        lines = lines[:cut]
    kept = [ln for ln in lines if not ln.lstrip().startswith(">")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
