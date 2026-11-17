import re

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def extract_json(text: str) -> str:
    """Tolerate code fences or chatter around a JSON object; return the object text."""
    t = _FENCE.sub("", text.strip()).strip()
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start != -1 and end > start else t
