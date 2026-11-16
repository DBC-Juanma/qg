"""Topic-name similarity: one change walked under slightly different names stays one topic."""

import re
import unicodedata

STOP_WORDS = frozenset(
    "a an and at by for from in into of on or the to with via per system systems feature page".split()
)
MIN_PREFIX = 4
THRESHOLD = 0.8


def _stem(word: str) -> str:
    """Strips a simple English plural."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(name: str) -> frozenset[str]:
    """Returns the name's lowercase, accent-free, stop-word-free, singularised word set.

    A hyphen inside a word joins it (`pre-score` is `prescore`), as separators already do for keys.
    """
    plain = unicodedata.normalize("NFKD", name.casefold()).encode("ascii", "ignore").decode()
    joined = re.sub(r"(?<=[a-z0-9])['\u2019-](?=[a-z0-9])", "", plain)
    words = (_stem(word) for word in re.findall(r"[a-z0-9]+", joined))
    return frozenset(word for word in words if word not in STOP_WORDS)


def _same_word(a: str, b: str) -> bool:
    """Tells whether two words are equal or one is a prefix (4+ letters) of the other."""
    short, long = sorted((a, b), key=len)
    return short == long or (len(short) >= MIN_PREFIX and long.startswith(short))


def similarity(a: str, b: str) -> float:
    """Returns the share of the smaller name's words the other name has."""
    left, right = tokens(a), tokens(b)
    if not left or not right:
        return 0.0
    small, big = sorted((left, right), key=len)
    hits = sum(1 for word in small if any(_same_word(word, other) for other in big))
    return hits / len(small)


def similar(a: str, b: str) -> bool:
    """Tells whether two topic names are the same topic.

    A one-word name only matches an equal word set; longer names match at THRESHOLD overlap.
    """
    left, right = tokens(a), tokens(b)
    if min(len(left), len(right)) < 2:
        return bool(left) and left == right
    return similarity(a, b) >= THRESHOLD


def closest(name: str, candidates: list[str]) -> str | None:
    """Returns the one candidate most similar to `name`, None when none is or the best ties."""
    scored = sorted(
        ((similarity(name, c), c) for c in candidates if similar(name, c)), reverse=True
    )
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]
