"""
NBA Stat Correction Discord Bot
Monitors live NBA play-by-play for stat corrections and posts them to Discord.
"""

import time
import sqlite3
import requests
import os
from datetime import datetime, timezone
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discordapp.com/api/webhooks/1486803328766574753/lVrHREVTqTkWiKl0rL1LKx8RuGZkJdD3IE1ZXXZ1YQi9z77IEzIeesP_GKLLn5r1lxgo")
POLL_INTERVAL_SECONDS = 10
DB_PATH = os.environ.get("DB_PATH", "corrections.db")

# A change that shows up this soon after the play was first recorded is just the
# feed filling the play in (e.g. the assist arriving one poll after the shot),
# not a real correction. Those are logged to the database but never posted.
MIN_CORRECTION_SECONDS = float(os.environ.get("MIN_CORRECTION_SECONDS", "15"))

# A play that vanishes from the feed is only reported as deleted once it has been
# missing this many polls in a row, and never on a poll where more than a handful
# of plays are missing at once (a truncated response, not real deletions).
DELETED_AFTER_POLLS = int(os.environ.get("DELETED_AFTER_POLLS", "3"))
TRUNCATED_FEED_MAX_MISSING = 5

# Color codes for Discord embeds
COLORS = {
    "removed": 0xE74C3C,   # red
    "added":   0x2ECC71,   # green
    "mixup":   0xF1C40F,   # yellow
}
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS corrections (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id            TEXT,
            play_id            TEXT,
            player             TEXT,
            stat               TEXT,
            old_value          TEXT,
            new_value          TEXT,
            description        TEXT,
            period             INTEGER,
            clock              TEXT,
            detected_at        TEXT,
            seconds_to_correct REAL,
            correction_key     TEXT UNIQUE
        )
    """)
    conn.commit()
    conn.close()
    print(f"[db] Database ready: {DB_PATH}")

def already_reported(correction_key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM corrections WHERE correction_key = ?", (correction_key,))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_correction(game_id, play_id, player, stat, old_val, new_val,
                    description, period, clock, detected_at, seconds_to_correct,
                    correction_key):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO corrections
            (game_id, play_id, player, stat, old_value, new_value, description,
             period, clock, detected_at, seconds_to_correct, correction_key)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (game_id, play_id, player, stat, str(old_val), str(new_val),
              description, period, clock, detected_at, seconds_to_correct,
              correction_key))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False  # already saved — do not post
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://www.nba.com/",
    "Origin":     "https://www.nba.com",
    "Accept":     "application/json",
}

# Where the live data files are served from. cdn.nba.com started answering 403
# (Access Denied), so the storage bucket behind it is tried first; whichever
# source last worked is tried first on the next request.
FEED_BASES = [
    "https://nba-prod-us-east-1-mediaops-stats.s3.amazonaws.com/NBA/liveData",
    "https://cdn.nba.com/static/json/liveData",
]

def fetch_feed(path):
    """GET a live-data file, falling back across FEED_BASES. Raises if all fail."""
    last_error = None
    for base in list(FEED_BASES):
        try:
            r = requests.get(f"{base}/{path}", headers=HEADERS, timeout=10)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            last_error = e
            continue
        if FEED_BASES[0] != base:
            FEED_BASES.remove(base)
            FEED_BASES.insert(0, base)
            print(f"[feed] now using {base}")
        return data
    raise last_error

def get_live_scoreboard():
    try:
        data = fetch_feed("scoreboard/todaysScoreboard_00.json")
        games = data.get("scoreboard", {}).get("games", [])
        return [g for g in games if g.get("gameStatus") == 2]
    except Exception as e:
        print(f"[scoreboard error] {e}")
        return []

def get_play_by_play(game_id):
    try:
        data = fetch_feed(f"playbyplay/playbyplay_{game_id}.json")
        actions = data.get("game", {}).get("actions", [])
        return {str(a["actionNumber"]): a for a in actions}
    except Exception as e:
        print(f"[pbp error game {game_id}] {e}")
        return {}
STAT_NAMES = {"ast": "assist", "reb": "rebound"}

def classify_correction(stat, old_id, new_id):
    """old_id / new_id are the player ids credited before and after (0/None = nobody)."""
    name = STAT_NAMES[stat]
    if old_id and not new_id:
        return "removed", f"{name} removed"
    if not old_id and new_id:
        return "added", f"{name} added"
    return "mixup", f"{name} mixup"

def is_rebound(play):
    return play.get("actionType") == "rebound"

def diff_plays(old_play, new_play):
    """Compare two versions of the same play. Returns [(stat, old_player_id, new_player_id)]."""
    diffs = []

    # Assist: who is credited with the assist on a made shot.
    old_ast = old_play.get("assistPersonId") or None
    new_ast = new_play.get("assistPersonId") or None
    if old_ast != new_ast:
        diffs.append(("ast", old_ast, new_ast))

    # Rebound: who is credited on a rebound play. Team rebounds carry player id 0,
    # so team -> player reads as "added" and player -> team as "removed".
    if is_rebound(old_play) or is_rebound(new_play):
        old_reb = (old_play.get("personId") or None) if is_rebound(old_play) else None
        new_reb = (new_play.get("personId") or None) if is_rebound(new_play) else None
        if old_reb != new_reb:
            diffs.append(("reb", old_reb, new_reb))

    return diffs

def credits_lost_if_deleted(play):
    """What a play credits, as [(stat, player_id)], i.e. what is taken away if the
    play is deleted from the feed outright."""
    lost = []
    if play.get("assistPersonId"):
        lost.append(("ast", play["assistPersonId"]))
    if is_rebound(play) and play.get("personId"):
        lost.append(("reb", play["personId"]))
    return lost

def find_deleted_plays(old_snap, plays, missing, now):
    """Track plays that have dropped out of the feed. `missing` maps play id ->
    (polls missing, time first missing) and is updated in place. Returns the ids
    that have now been gone DELETED_AFTER_POLLS polls in a row."""
    for pid in list(missing):
        if pid in plays:
            del missing[pid]  # it came back; it was a feed hiccup
    if sum(1 for pid in old_snap if pid not in plays) > TRUNCATED_FEED_MAX_MISSING:
        missing.clear()
        return []
    deleted = []
    for pid, (old_play, _first_seen, _polls) in old_snap.items():
        if pid in plays or not credits_lost_if_deleted(old_play):
            continue
        count, since = missing.get(pid, (0, now))
        count += 1
        missing[pid] = (count, since)
        if count >= DELETED_AFTER_POLLS:
            deleted.append(pid)
    return deleted

def too_fast(elapsed, polls_seen):
    """True if the change arrived on the very next poll after the play first
    appeared, or inside MIN_CORRECTION_SECONDS. That is feed lag, not a correction."""
    return polls_seen <= 1 or elapsed < MIN_CORRECTION_SECONDS
def ordinal(n):
    return {1:"1st", 2:"2nd", 3:"3rd", 4:"4th"}.get(n, f"{n}th")

def dot_color(ctype):
    return {"removed": "🔴", "added": "🟢", "mixup": "🟡"}.get(ctype, "⚪")

def credited_name(play, stat):
    """Name of the player a play credits with the assist / rebound, or None."""
    if stat == "ast":
        return play.get("assistPlayerNameInitial") or None
    if is_rebound(play) and play.get("personId"):
        return play.get("playerNameI") or None
    return None

def post_to_discord(game, play, old_play, correction_type, label, stat,
                    old_val, new_val, seconds_elapsed):
    period_str = ordinal(play.get("period", 0))
    clock      = play.get("clock", "").replace("PT","").replace("M","m ").replace("S","s")
    game_code  = game.get("gameCode", "?").replace("/", " vs ")
    play_num   = play.get("actionNumber", "?")
    desc       = play.get("description", "")

    old_name = credited_name(old_play, stat) or str(old_val or "none")
    new_name = credited_name(play, stat) or str(new_val or "none")
    if stat == "reb":
        # Headline the player whose rebound count changed; team rebounds have no name.
        player = (credited_name(play, stat) or credited_name(old_play, stat)
                  or play.get("teamTricode") or "Team")
    else:
        player = play.get("playerNameI", "Unknown")

    if correction_type == "mixup":
        change_line = f"❌ Taken from: **{old_name}**\n✅ Given to: **{new_name}**"
    elif correction_type == "removed":
        change_line = f"❌ Removed from: **{old_name}**"
    else:
        change_line = f"✅ Added to: **{new_name}**"

    mins = int(seconds_elapsed // 60)
    secs = int(seconds_elapsed % 60)
    time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

    embed = {
        "embeds": [{
            "description": (
                f"{dot_color(correction_type)} **{player}** — {label}\n"
                f"```{desc}```\n"
                f"**{period_str} {clock}** · {game_code} · play #{play_num}\n"
                f"{change_line}\n"
                f"⏱ corrected {time_str} after recorded"
            ),
            "color": COLORS.get(correction_type, 0xAAAAAA),
            "footer": {"text": f"NBA Correction Bot · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}
        }]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[discord error] {e}")
def run():
    init_db()
    print("🏀 NBA Correction Bot started. Polling every "
          f"{POLL_INTERVAL_SECONDS}s for live games...\n")

    snapshots = {}
    missing = {}  # game_id -> {play id: (polls missing, time first missing)}

    while True:
        live_games = get_live_scoreboard()

        if not live_games:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] No live games right now. "
                  "Checking again soon...")
        else:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"{len(live_games)} live game(s) found.")

        for game in live_games:
            game_id   = game["gameId"]
            game_code = game.get("gameCode", game_id)

            plays = get_play_by_play(game_id)
            if not plays:
                continue

            if game_id not in snapshots:
                snapshots[game_id] = {pid: (p, time.time(), 0) for pid, p in plays.items()}
                print(f"  Tracking new game: {game_code} ({len(plays)} plays)")
                continue

            old_snap = snapshots[game_id]

            for pid, new_play in plays.items():
                if pid not in old_snap:
                    old_snap[pid] = (new_play, time.time(), 0)
                    continue

                old_play, first_seen, polls_seen = old_snap[pid]
                polls_seen += 1
                diffs = diff_plays(old_play, new_play)

                for (stat, old_v, new_v) in diffs:
                    correction_key = f"{game_id}_{pid}_{stat}_{old_v}_{new_v}"

                    if already_reported(correction_key):
                        continue

                    ctype, label = classify_correction(stat, old_v, new_v)
                    elapsed = time.time() - first_seen
                    # This guarantees no duplicates even if bot crashes mid-post
                    saved = save_correction(
                        game_id, pid,
                        new_play.get("playerNameI", "?"),
                        stat, old_v, new_v,
                        new_play.get("description", ""),
                        new_play.get("period", 0),
                        new_play.get("clock", ""),
                        datetime.now(timezone.utc).isoformat(),
                        elapsed,
                        correction_key
                    )

                    if not saved:
                        # Already in database — skip posting
                        continue

                    if too_fast(elapsed, polls_seen):
                        print(f"  (skipped, feed lag {elapsed:.0f}s) "
                              f"{new_play.get('playerNameI','?')} — {label} ({game_code})")
                        continue

                    print(f"  ✅ CORRECTION: {new_play.get('playerNameI','?')} "
                          f"— {label} ({game_code})")

                    post_to_discord(game, new_play, old_play, ctype, label,
                                    stat, old_v, new_v, elapsed)

                old_snap[pid] = (new_play, first_seen, polls_seen)

            # Plays deleted from the feed outright: whoever they credited loses it.
            game_missing = missing.setdefault(game_id, {})
            for pid in find_deleted_plays(old_snap, plays, game_missing, time.time()):
                old_play, first_seen, polls_seen = old_snap.pop(pid)
                _, missing_since = game_missing.pop(pid)
                elapsed = missing_since - first_seen

                for (stat, old_v) in credits_lost_if_deleted(old_play):
                    label = f"{STAT_NAMES[stat]} removed (play deleted)"
                    saved = save_correction(
                        game_id, pid,
                        old_play.get("playerNameI", "?"),
                        stat, old_v, None,
                        old_play.get("description", ""),
                        old_play.get("period", 0),
                        old_play.get("clock", ""),
                        datetime.now(timezone.utc).isoformat(),
                        elapsed,
                        f"{game_id}_{pid}_{stat}_{old_v}_deleted"
                    )
                    if not saved:
                        continue

                    if too_fast(elapsed, polls_seen + 1):
                        print(f"  (skipped, feed lag {elapsed:.0f}s) "
                              f"{old_play.get('playerNameI','?')} — {label} ({game_code})")
                        continue

                    print(f"  ✅ CORRECTION: {old_play.get('playerNameI','?')} "
                          f"— {label} ({game_code})")

                    post_to_discord(game, old_play, old_play, "removed", label,
                                    stat, old_v, None, elapsed)

            snapshots[game_id] = old_snap

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run()
