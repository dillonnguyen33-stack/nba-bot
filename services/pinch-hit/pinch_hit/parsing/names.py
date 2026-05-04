import unicodedata

_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_last_name(full_name: str) -> str:
    """NFD-decompose, strip accents, strip suffixes, lowercase."""
    parts = full_name.strip().split()
    if not parts:
        return ""
    if len(parts) > 1 and parts[-1].rstrip(".").lower() in _NAME_SUFFIXES:
        parts = parts[:-1]
    last = parts[-1]
    decomposed = unicodedata.normalize("NFD", last)
    return decomposed.encode("ascii", "ignore").decode("ascii").lower()
