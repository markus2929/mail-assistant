import re

# English + Dutch/German reply and forward prefixes: Re, Fw, Fwd, Aw, Antw, Vs, Wg, Doorgestuurd
_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|antw|vs|wg|doorgestuurd|doorst)(?:\[\d+\])?\s*:\s*)+",
                     re.IGNORECASE)


def normalise_subject(subject: str | None) -> tuple[str, bool]:
    """Return (normalised subject, whether a reply/forward prefix was removed)."""
    s = subject or ""
    m = _PREFIX.match(s)
    if m:
        s = s[m.end():]
    return re.sub(r"\s+", " ", s).strip().lower(), bool(m)
