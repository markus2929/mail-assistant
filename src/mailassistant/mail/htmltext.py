"""HTML -> plain text for LLM input.

Drops script/style and visually hidden text (a classic place to hide prompt injections), strips
invisible/bidi control characters, and reports how much hidden text was removed.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urlparse

_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source",
         "track", "wbr"}
_DROP = {"script", "style", "head", "title", "noscript", "template", "svg", "object", "iframe"}
_BLOCK = {"p", "div", "tr", "li", "ul", "ol", "table", "blockquote", "section", "article",
          "header", "footer", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
_HIDDEN = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![\d.])|"
    r"opacity\s*:\s*0(?:\.0+)?(?![\d.])|max-height\s*:\s*0(?![\d.])|mso-hide\s*:\s*all", re.I)
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\u00ad]")
_MEETING_HOSTS = ("zoom.us", "teams.microsoft.com", "teams.live.com", "meet.google.com",
                  "webex.com", "gotomeeting.com", "whereby.com", "calendly.com", "jit.si")


def _is_meeting_link(href: str) -> bool:
    try:
        host = (urlparse(href).hostname or "").lower()
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _MEETING_HOSTS)


def clean_text(s: str) -> str:
    s = _INVISIBLE.sub("", s.replace("\x00", "")).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[tuple[str, bool, bool, str]] = []  # (tag, drop, hidden, href)
        self.hidden_chars = 0

    def _nl(self) -> None:
        if self.out and not self.out[-1].endswith("\n"):
            self.out.append("\n")

    def _suppressed(self) -> bool:
        return bool(self.stack) and (self.stack[-1][1] or self.stack[-1][2])

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        if tag in _VOID:
            if tag in ("br", "hr") and not self._suppressed():
                self.out.append("\n")
            return
        if tag == "body":  # an unclosed <head> must not swallow the body
            self.stack = [e for e in self.stack if e[0] not in _DROP]
        _, p_drop, p_hid, _ = self.stack[-1] if self.stack else ("", False, False, "")
        drop = p_drop or tag in _DROP
        hid = p_hid or "hidden" in a or bool(_HIDDEN.search(a.get("style", "")))
        self.stack.append((tag, drop, hid, a.get("href", "") if tag == "a" else ""))
        if not drop and not hid:
            if tag in _BLOCK:
                self._nl()
            elif tag in ("td", "th"):
                self.out.append(" ")
            if tag == "li":
                self.out.append("- ")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                _, drop, hid, href = self.stack[i]
                del self.stack[i:]
                break
        else:
            return
        if drop or hid:
            return
        if tag == "a" and href and _is_meeting_link(href):
            self.out.append(f" ({href}) ")  # keep join links: needed for calendar extraction
        if tag in _BLOCK:
            self._nl()

    def handle_data(self, data):
        if not self.stack:
            self.out.append(data)
            return
        _, drop, hid, _ = self.stack[-1]
        if drop:
            return
        if hid:
            self.hidden_chars += len(data.strip())
            return
        self.out.append(data)


def html_to_text(html: str) -> tuple[str, int]:
    """Return (text, number of hidden-text characters that were removed)."""
    p = _Extractor()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - never fail a message because of odd HTML
        return clean_text(re.sub(r"<[^>]+>", " ", html)), 0
    text = clean_text("".join(p.out))
    if not text and len(html) > 40:
        # e.g. an unclosed <title> made the parser swallow the whole document: crude fallback
        crude = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", html)
        text = clean_text(re.sub(r"<[^>]*>?", " ", crude))
    return text, p.hidden_chars
