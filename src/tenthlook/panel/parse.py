"""Strict answer parsing. The *last* `ANSWER:` line wins; anything else is unparseable, never guessed."""
from __future__ import annotations

import re

_PATTERNS = {
    "int": re.compile(r"^\s*\**\s*ANSWER\s*:\s*\**\s*(-?[0-9][0-9,]*)\s*\**\s*\.?\s*$", re.I | re.M),
    "choice": re.compile(r"^\s*\**\s*ANSWER\s*:\s*\**\s*\(?([AB])\)?\s*\**\s*\.?\s*$", re.I | re.M),
    "yesno": re.compile(r"^\s*\**\s*ANSWER\s*:\s*\**\s*(YES|NO)\s*\**\s*\.?\s*$", re.I | re.M),
}


def parse_answer(kind: str, text: str) -> str | None:
    """Canonical answer or None. Tolerates markdown bold and a trailing period, nothing more."""
    matches = _PATTERNS[kind].findall(text or "")
    if not matches:
        return None
    val = matches[-1].upper()
    if kind == "int":
        val = val.replace(",", "")
        try:
            return str(int(val))
        except ValueError:
            return None
    return val
