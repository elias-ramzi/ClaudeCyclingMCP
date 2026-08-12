"""ASCII folding for on-screen workout text.

MyWhoosh renders accented characters and typographic punctuation badly in-game,
so messages are folded to plain ASCII before they reach a `.zwo` file. The fold
is reported rather than silent: callers surface it as a warning so the author
can reword instead of accepting a mangled string.
"""

from __future__ import annotations

import unicodedata

# Characters that NFKD will not decompose into ASCII on its own.
_SUBSTITUTIONS = {
    "‘": "'",  # left single quote
    "’": "'",  # right single quote / typographic apostrophe
    "‚": "'",
    "‛": "'",
    "“": '"',  # left double quote
    "”": '"',  # right double quote
    "„": '"',
    "–": "-",  # en dash
    "—": "-",  # em dash
    "―": "-",
    "−": "-",  # minus sign
    "…": "...",  # ellipsis
    " ": " ",  # non-breaking space
    " ": " ",  # narrow no-break space
    "→": "->",  # right arrow
    "×": "x",  # multiplication sign
    "°": " deg",
    "æ": "ae",
    "Æ": "AE",
    "œ": "oe",
    "Œ": "OE",
    "ß": "ss",
    "ø": "o",
    "Ø": "O",
    "ł": "l",
    "Ł": "L",
}


def fold_to_ascii(text: str) -> str:
    """Return `text` reduced to printable ASCII."""
    replaced = "".join(_SUBSTITUTIONS.get(char, char) for char in text)
    decomposed = unicodedata.normalize("NFKD", replaced)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return "".join(char if 32 <= ord(char) < 127 else "?" for char in stripped)


def fold_with_warning(text: str, where: str) -> tuple[str, str | None]:
    """Fold `text`, returning the result and a warning when it changed."""
    folded = fold_to_ascii(text)
    if folded == text:
        return folded, None
    return folded, (
        f"{where}: non-ASCII text was folded for MyWhoosh "
        f"({text!r} -> {folded!r}); reword it if the result reads badly in-game"
    )
