"""Parse Devin's enumerated questions into tappable button options.

Devin asks structured questions mid-task ("Which approach? 1. … 2. …") and
answering on a phone means typing. This pulls the option list out of the
message text so the relay can attach one button per choice — a tap sends
the option back as a steering message.

Deliberately conservative: anything ambiguous returns None and the message
posts bare, exactly as before. A stray button row is the worst failure.
"""

from __future__ import annotations

import re

# Line-anchored list styles: "1. opt" / "1) opt", "A. opt" / "A) opt",
# "- opt" / "* opt".
_NUM_RE = re.compile(r"^\s*(\d+)[.)]\s+(\S.*?)\s*$")
_ALPHA_RE = re.compile(r"^\s*([A-Za-z])[.)]\s+(\S.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(\S.*?)\s*$")

MIN_OPTIONS = 2
MAX_OPTIONS = 5  # Discord caps action rows at 5 buttons
MAX_OPTION_LEN = 120

# Confirmation-shaped endings that warrant bare Yes/No buttons.
_CONFIRM_RE = re.compile(
    r"proceed|continue|should i|shall i|want me to|"
    r"would you like me to|ok to|go ahead",
    re.IGNORECASE,
)


def _classify(line: str) -> tuple[str, int, str] | None:
    """(style, seq, text) for a list line; seq is the 0-based position the
    marker claims (number-1 / letter index), -1 for bullets."""
    m = _NUM_RE.match(line)
    if m:
        return "num", int(m.group(1)) - 1, m.group(2)
    m = _ALPHA_RE.match(line)
    if m:
        return "alpha", ord(m.group(1).upper()) - ord("A"), m.group(2)
    m = _BULLET_RE.match(line)
    if m:
        return "bullet", -1, m.group(1)
    return None


def _runs(text: str):
    """Yield (kind, items) for each maximal run of consecutive same-style
    list lines."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        first = _classify(lines[i])
        if first is None:
            i += 1
            continue
        kind = first[0]
        items = [first]
        j = i + 1
        while j < len(lines):
            nxt = _classify(lines[j])
            if nxt is None or nxt[0] != kind:
                break
            items.append(nxt)
            j += 1
        yield kind, items
        i = j


def _qualifies(kind: str, items: list[tuple[str, int, str]]) -> list[str] | None:
    """Option texts when the run is a real menu, else None."""
    if not MIN_OPTIONS <= len(items) <= MAX_OPTIONS:
        return None
    if kind != "bullet":
        # Sequential numbering/lettering from 1/A — gaps or restarts mean
        # the list is prose ("1. do this … 3. then that"), not a menu.
        if [seq for _, seq, _ in items] != list(range(len(items))):
            return None
    texts = [t for _, _, t in items]
    if any(not 1 <= len(t) <= MAX_OPTION_LEN for t in texts):
        return None
    return texts


def parse_choices(text: str) -> list[str] | None:
    """Option texts for the message's closing question, or None."""
    if not text or "?" not in text:
        return None
    # The LAST qualifying block is the choice — earlier lists are context.
    options = None
    for kind, items in _runs(text):
        opts = _qualifies(kind, items)
        if opts is not None:
            options = opts
    if options is not None:
        return options
    # No list — a confirmation-shaped closing question still gets buttons.
    # The ? must be the message's last character: a mid-message question
    # followed by prose isn't asking for a tap.
    tail = text.rstrip().rsplit("\n", 1)[-1].strip()
    if tail.endswith("?") and _CONFIRM_RE.search(tail):
        return ["Yes", "No"]
    return None
