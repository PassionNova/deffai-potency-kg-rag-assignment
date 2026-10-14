import re

_WS = re.compile(r"\s+")
_TRANSLATE = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u00a0": " ",
    }
)


def collapse_ws(text: str) -> str:
    """Collapse PDF layout whitespace. Used for embedding and model-facing evidence, never for citations."""
    return _WS.sub(" ", text).strip()


def match_form(text: str) -> str:
    return collapse_ws(text.translate(_TRANSLATE)).lower()


def find_exact_span(original: str, quote: str) -> str | None:
    """Return the exact substring of `original` matching `quote` up to whitespace and quote-style differences."""
    tokens = [t for t in collapse_ws(quote).split(" ") if t]
    if not tokens:
        return None
    m = re.search(r"\s+".join(_token_pattern(t) for t in tokens), original, flags=re.IGNORECASE)
    return m.group(0) if m else None


_LINE_NO_GAP = r"\s+(?:\d{1,4}\s+)*"


def match_span(original: str, quote: str) -> tuple[str | None, list[str]]:
    """Exact span first; otherwise allow standalone 1-4 digit tokens of `original` between quote words (PDF line
    numbers). Returns (exact source substring, skipped number tokens); callers must surface skipped tokens."""
    exact = find_exact_span(original, quote)
    if exact is not None:
        return exact, []
    tokens = [t for t in collapse_ws(quote).split(" ") if t]
    if not tokens:
        return None, []
    m = re.search(_LINE_NO_GAP.join(_token_pattern(t) for t in tokens), original, flags=re.IGNORECASE)
    if not m:
        return None, []
    span = m.group(0)
    skipped = [t for t in collapse_ws(span).split(" ") if t.isdigit()]
    for t in tokens:
        if t.isdigit() and t in skipped:
            skipped.remove(t)
    return span, skipped


_VARIANTS = {"'": "['\u2018\u2019]", '"': '["\u201c\u201d]', "-": "[-\u2013\u2014]"}


def _token_pattern(token: str) -> str:
    return "".join(_VARIANTS.get(ch.translate(_TRANSLATE), re.escape(ch)) for ch in token)


def code_points(text: str) -> int:
    """Unicode code point count (Python len); the unit for evidence budgets."""
    return len(text)
