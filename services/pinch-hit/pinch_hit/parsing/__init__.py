PlayerPair = tuple[str | None, str | None]

from pinch_hit.parsing.tweet import TweetResult  # noqa: E402 — after PlayerPair to avoid circular import

__all__ = ["PlayerPair", "TweetResult"]
