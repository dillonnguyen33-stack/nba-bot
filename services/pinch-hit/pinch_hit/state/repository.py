import logging
from typing import cast

from pinch_hit.state.db import get_db
from pinch_hit.state.types import PendingAlertRow
from pinch_hit.types import AlertStatus, STATUS_CONFIRMED, STATUS_PENDING, STATUS_TIMEOUT

logger = logging.getLogger(__name__)


async def insert_pending_alert(
    discord_message_id: str,
    pinch_hitter_raw: str,
    pinch_hitter_normalized: str,
    team_id: int,
    tweet_id: str,
    replaced_player: str | None = None,
    game_pk: int | None = None,
) -> int:
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO pending_alerts
           (discord_message_id, pinch_hitter_raw, pinch_hitter_normalized,
            replaced_player, team_id, game_pk, tweet_id, posted_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)""",
        (discord_message_id, pinch_hitter_raw, pinch_hitter_normalized,
         replaced_player, team_id, game_pk, tweet_id, STATUS_PENDING),
    )
    await db.commit()
    if cursor.lastrowid is None:
        raise RuntimeError("insert_pending_alert: no row ID returned")
    return cursor.lastrowid


async def get_pending_alerts_by_team(team_id: int) -> list[PendingAlertRow]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM pending_alerts WHERE status = ? AND team_id = ?",
        (STATUS_PENDING, team_id),
    ) as cursor:
        rows = await cursor.fetchall()
    return [cast(PendingAlertRow, dict(row)) for row in rows]


async def update_alert_status(alert_id: int, status: AlertStatus, game_pk: int | None = None) -> None:
    db = await get_db()
    if status == STATUS_CONFIRMED:
        await db.execute(
            "UPDATE pending_alerts SET status = ?, confirmed_at = datetime('now'), game_pk = COALESCE(?, game_pk) WHERE id = ?",
            (status, game_pk, alert_id),
        )
    else:
        await db.execute(
            "UPDATE pending_alerts SET status = ? WHERE id = ?",
            (status, alert_id),
        )
    await db.commit()


async def get_expired_pending_alerts(timeout_minutes: int) -> list[PendingAlertRow]:
    db = await get_db()
    async with db.execute(
        """SELECT * FROM pending_alerts
           WHERE status = ?
           AND posted_at < datetime('now', ? || ' minutes')""",
        (STATUS_PENDING, f"-{timeout_minutes}"),
    ) as cursor:
        rows = await cursor.fetchall()
    return [cast(PendingAlertRow, dict(row)) for row in rows]


async def bulk_update_alerts_timeout(alert_ids: list[int]) -> None:
    if not alert_ids:
        return
    db = await get_db()
    placeholders = ",".join("?" * len(alert_ids))
    await db.execute(
        f"UPDATE pending_alerts SET status = ? WHERE id IN ({placeholders})",
        [STATUS_TIMEOUT, *alert_ids],
    )
    await db.commit()


async def try_claim_tweet(tweet_id: str) -> bool:
    """Atomically claim a tweet_id. Returns True if this caller won the race."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO seen_tweets (tweet_id) VALUES (?)",
        (tweet_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def is_tweet_seen(tweet_id: str) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT 1 FROM seen_tweets WHERE tweet_id = ?", (tweet_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def nightly_cleanup() -> None:
    """Prevent unbounded DB growth on the Railway persistent volume."""
    db = await get_db()

    cleanup_queries = [
        ("seen_tweets", "DELETE FROM seen_tweets WHERE seen_at < datetime('now', '-1 day')"),
        ("pending_alerts", """DELETE FROM pending_alerts
           WHERE status IN (?, ?)
           AND posted_at < datetime('now', '-7 days')"""),
        ("evaluation_log", "DELETE FROM evaluation_log WHERE timestamp < datetime('now', '-30 days')"),
    ]

    failed = False
    for table, query in cleanup_queries:
        try:
            if table == "pending_alerts":
                await db.execute(query, (STATUS_CONFIRMED, STATUS_TIMEOUT))
            else:
                await db.execute(query)
            await db.commit()
        except Exception:
            logger.exception("nightly cleanup failed for %s", table)
            failed = True
    if failed:
        raise RuntimeError("nightly cleanup had partial failures")
