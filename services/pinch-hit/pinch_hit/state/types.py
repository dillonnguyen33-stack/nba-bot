from typing import TypedDict

from pinch_hit.types import AlertStatus


class PendingAlertRow(TypedDict):
    id: int
    discord_message_id: str
    pinch_hitter_raw: str
    pinch_hitter_normalized: str
    replaced_player: str | None
    team_id: int
    game_pk: int | None
    tweet_id: str
    posted_at: str
    confirmed_at: str | None
    status: AlertStatus


class QualifyingGame(TypedDict):
    game_pk: int
    home_team_id: int
    away_team_id: int
    home_team_name: str
    away_team_name: str
    inning: int
