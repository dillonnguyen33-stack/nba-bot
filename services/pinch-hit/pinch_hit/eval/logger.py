import json
import logging
from typing import Any

from pinch_hit.state.background import schedule_background
from pinch_hit.state.db import get_db
from pinch_hit.types import EventType

logger = logging.getLogger(__name__)


async def _write_row(
    event_type: EventType,
    source: str,
    game_pk: int | None,
    pinch_hitter: str | None,
    team_id: int | None,
    raw_payload: str | None,
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO evaluation_log
           (event_type, source, game_pk, pinch_hitter, team_id, raw_payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_type, source, game_pk, pinch_hitter, team_id, raw_payload),
    )
    await db.commit()


def log_event(
    event_type: EventType,
    source: str,
    game_pk: int | None = None,
    pinch_hitter: str | None = None,
    team_id: int | None = None,
    raw_payload: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget — caller never awaits the write."""
    payload_str = json.dumps(raw_payload) if raw_payload is not None else None
    schedule_background(
        _write_row(event_type, source, game_pk, pinch_hitter, team_id, payload_str),
        "eval_log",
    )
