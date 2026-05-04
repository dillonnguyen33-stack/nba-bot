import re

_GUMBO_PH_PATTERN = re.compile(
    r'Offensive Substitution: Pinch-hitter\s+(.+?)\s+replaces\s+(.+?)\.?\s*$',
    re.IGNORECASE,
)


def parse_gumbo_substitution(description: str) -> tuple[str, str] | None:
    """
    >>> parse_gumbo_substitution('Offensive Substitution: Pinch-hitter Joc Pederson replaces Freddie Freeman.')
    ('Joc Pederson', 'Freddie Freeman')
    """
    m = _GUMBO_PH_PATTERN.search(description)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()
