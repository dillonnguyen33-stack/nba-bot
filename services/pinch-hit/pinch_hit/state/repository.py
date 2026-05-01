from pinch_hit.state.db import get_db


# ── pending_alerts ─────────────────────────────────────────────────────────────

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
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 'pending')""",
        (discord_message_id, pinch_hitter_raw, pinch_hitter_normalized,
         replaced_player, team_id, game_pk, tweet_id),
    )
    await db.commit()
    return cursor.lastrowid


async def get_pending_alerts_by_team(team_id: int) -> list[dict]:
    db = await get_db()
    async with db.execute(
        "SELECT * FROM pending_alerts WHERE status = 'pending' AND team_id = ?",
        (team_id,),
    ) as cursor:
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]


async def update_alert_status(alert_id: int, status: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE pending_alerts SET status = ?, confirmed_at = datetime('now') WHERE id = ?",
        (status, alert_id),
    )
    await db.commit()


# ── seen_tweets ────────────────────────────────────────────────────────────────

async def insert_seen_tweet(tweet_id: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT OR IGNORE INTO seen_tweets (tweet_id) VALUES (?)",
        (tweet_id,),
    )
    await db.commit()


async def is_tweet_seen(tweet_id: str) -> bool:
    db = await get_db()
    async with db.execute(
        "SELECT 1 FROM seen_tweets WHERE tweet_id = ?", (tweet_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


# ── evaluation_log ─────────────────────────────────────────────────────────────

async def insert_eval_row(
    event_type: str,
    source: str,
    game_pk: int | None = None,
    pinch_hitter: str | None = None,
    team_id: int | None = None,
    raw_payload: str | None = None,
) -> None:
    db = await get_db()
    await db.execute(
        """INSERT INTO evaluation_log
           (event_type, source, game_pk, pinch_hitter, team_id, raw_payload)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (event_type, source, game_pk, pinch_hitter, team_id, raw_payload),
    )
    await db.commit()
