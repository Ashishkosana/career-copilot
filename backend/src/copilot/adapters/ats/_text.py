"""HTML → plain text for job descriptions.

Two source quirks make this less trivial than it looks:

* **Greenhouse double-encodes.** ``content`` arrives as escaped markup
  (``&lt;div class=&quot;…&quot;&gt;``), so a single unescape yields tags, and a
  second pass is needed before stripping. We unescape until it stops changing
  (bounded), then strip.
* **Nothing may be truncated.** Sponsorship, clearance and citizenship language
  lives in the *legal tail* at the very end of a JD. An earlier version of this
  pipeline cut descriptions at 4,000 characters and every eligibility gate
  silently measured nothing. Truncate downstream if you must, never here.
"""
from __future__ import annotations

import html
import re

_SCRIPTY = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|li|ul|ol|h[1-6]|tr|table|section)\s*>", re.I)
_BREAK = re.compile(r"<(br|hr)\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v]+")
_NEWLINES = re.compile(r"\n{3,}")

_MAX_UNESCAPE_PASSES = 3


def html_to_text(raw: str | None) -> str:
    """Collapse HTML (possibly escaped more than once) into readable plain text."""
    if not raw:
        return ""
    text = raw
    for _ in range(_MAX_UNESCAPE_PASSES):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _SCRIPTY.sub(" ", text)
    text = _BREAK.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = _SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _NEWLINES.sub("\n\n", text).strip()
