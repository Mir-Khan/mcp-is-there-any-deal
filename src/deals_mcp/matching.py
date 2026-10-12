"""Ranks ITAD search results by how closely their titles match the query.

ITAD's own ordering is fuzzy, so a plain query such as "Final Fantasy" puts XV and the VII remake
ahead of the original game. These functions normalize titles (case, punctuation, trademark
symbols, accents, and Roman numerals against Arabic ones), so that the closest titles come first
and each result says how well it matched.
"""

import re
import unicodedata

# Ordered from best to worst.
MATCH_LEVELS = (
    "exact",        # the same title once normalized, e.g. "final fantasy 7" and "FINAL FANTASY VII"
    "variant",      # exact apart from a trailing parenthetical ("Resident Evil 4 (2005)") or a single-letter numeral ("Mega Man X" for "mega man 10")
    "starts_with",  # the title starts with the query's words, e.g. "DOOM Eternal" for "doom"
    "contains",     # the title contains all of the query's words in any order
    "weak",         # some other fuzzy match from ITAD, which is most likely not what was meant
)

# Base games first, then bundles, then DLC and anything without a type (such as soundtracks).
TYPE_RANK = {"game": 0, "package": 1, "dlc": 2}


def _to_roman(n: int) -> str:
    numerals = [(10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
    out = ""
    for value, symbol in numerals:
        while n >= value:
            out += symbol
            n -= value
    return out


# Only whole words that are valid numerals from 1 to 30 are converted, so words such as "mix" or
# "civ" are left alone.
ROMAN_TO_ARABIC = {_to_roman(n): str(n) for n in range(1, 31)}
# The single letters are often part of a name rather than a number ("Mega Man X" is not Mega Man
# 10), so a match that depends on converting them is only treated as a variant, which is confirmed
# with the user, while longer numerals such as "vii" are trusted.
MULTI_LETTER_NUMERALS = {k: v for k, v in ROMAN_TO_ARABIC.items() if len(k) > 1}

# Apostrophes are dropped rather than turned into spaces, so "Assassin's" matches "Assassins".
SYMBOLS = str.maketrans({"™": " ", "®": " ", "©": " ", "&": " and ", "�": " ", "'": "", "’": "", "‘": ""})
TRAILING_PARENTHETICAL = re.compile(r"\s*\([^()]*\)\s*$")


def fold(text: str) -> str:
    """Casefold text, remove accents, and collapse whitespace, for comparing titles loosely.

    Letters from every script are kept, so titles such as "原神" or "Смута" still compare; NFKD
    also makes the composed and decomposed forms of an accented letter compare as equal.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).casefold().split())


def normalize(title: str, numerals: dict[str, str] = ROMAN_TO_ARABIC) -> str:
    """Fold a title, strip symbols and punctuation, and turn Roman numerals into numbers."""
    words = re.sub(r"[\W_]+", " ", fold(title.translate(SYMBOLS))).split()
    return " ".join(numerals.get(w, w) for w in words)


def _compact(title: str, numerals: dict[str, str] = ROMAN_TO_ARABIC) -> str:
    # Comparing with the spaces removed also matches "Spiderman" against "Spider-Man".
    return normalize(title, numerals).replace(" ", "")


def strip_suffix(title: str) -> str:
    """Remove trailing parentheticals such as "(2005)" or "(3D Remake)"."""
    previous = None
    while previous != title:
        previous, title = title, TRAILING_PARENTHETICAL.sub("", title)
    return title


def match_level(query: str, title: str) -> str:
    query_words = normalize(query).split()
    title_words = normalize(title).split()
    # A query with no letters or digits (such as "™" or "!!!") would otherwise be a prefix of every title.
    if not query_words:
        return "weak"
    if _compact(query, MULTI_LETTER_NUMERALS) == _compact(title, MULTI_LETTER_NUMERALS):
        return "exact"
    joined_query = "".join(query_words)
    if joined_query == "".join(title_words) or joined_query == _compact(strip_suffix(title)):
        return "variant"
    if title_words[: len(query_words)] == query_words:
        return "starts_with"
    if set(query_words) <= set(title_words):
        return "contains"
    return "weak"


def rank_results(query: str, results: list[dict]) -> list[tuple[dict, str]]:
    """Sort results by match level and then by type, keeping ITAD's order within each group."""
    scored = [(r, match_level(query, r["title"])) for r in results]
    scored.sort(key=lambda pair: (MATCH_LEVELS.index(pair[1]), TYPE_RANK.get(pair[0].get("type"), 3)))
    return scored
