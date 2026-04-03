"""
NBA Stat Correction Discord Bot
Monitors live NBA play-by-play for stat corrections and posts them to Discord.
"""

import time
import sqlite3
import requests
import os
from datetime import datetime

# ── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discordapp.com/api/webhooks/1486803328766574753/lVrHREVTqTkWiKl0rL1LKx8RuGZkJdD3IE1ZXXZ1YQi9z77IEzIeesP_GKLLn5r1lxgo")
POLL_INTERVAL_SECONDS = 10
DB_PATH = os.environ.get("DB_PATH", "corrections.db")

# Stat types to monitor
WATCH_STATS = {"ast", "pts", "reb"}

# Color codes for Discord embeds
COLORS = {
    "removed": 0xE74C3C,   # red
    "added":   0x2ECC71,   # green
    "mixup":   0xF1C40F,   # yellow
    "points":  0x9B59B6,   # purple
    "rebound": 0x3498DB,   # blue
}

# ── DATABASE ─────────────────────────────────────────────────────────────────
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
    """Check if this correction was already posted (survives restarts)."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM corrections WHERE correction_key = ?", (correction_key,))
    result = c.fetchone()
    conn.close()
    return result is not None

def save_correction(game_id, play_id, player, stat, old_val, new_val,
                    description, period, clock, detected_at, seconds_to_correct,
                    correction_key):
    """
    Save correction to database FIRST before posting to Discord.
    Returns True if saved successfully, False if already exists.
    """
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

# ── NBA API ───────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://www.nba.com/",
    "Origin":     "https://www.nba.com",
    "Accept":     "application/json",
}

def get_live_scoreboard():
    url = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = data.get("scoreboard", {}).get("games", [])
        return [g for g in games if g.get("gameStatus") == 2]
    except Exception as e:
        print(f"[scoreboard error] {e}")
        return []

def get_play_by_play(game_id):
    url = f"https://cdn.nba.com/static/json/liveData/playbyplay/playbyplay_{game_id}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        actions = data.get("game", {}).get("actions", [])
        return {str(a["actionNumber"]): a for a in actions}
    except Exception as e:
        print(f"[pbp error game {game_id}] {e}")
        return {}

# ── CORRECTION DETECTION ─────────────────────────────────────────────────────
def classify_correction(stat, old_val, new_val, old_player, new_player):
    if stat == "ast":
        if old_player and not new_player:
            return "removed", "assist removed"
        if not old_player and new_player:
            return "added", "assist added"
        if old_player != new_player:
            return "mixup", "assist mixup"
    if stat == "pts":
        return "points", f"points corrected ({old_val} → {new_val})"
    if stat == "reb":
        return "rebound", f"rebound corrected ({old_val} → {new_val})"
    return "added", f"{stat} corrected ({old_val} → {new_val})"

def diff_plays(old_play, new_play):
    diffs = []
    for stat in WATCH_STATS:
        old_v = old_play.get(stat)
        new_v = new_play.get(stat)
        if old_v != new_v:
            diffs.append((stat, old_v, new_v))

    old_ast = old_play.get("assistPersonId")
    new_ast = new_play.get("assistPersonId")
    if old_ast != new_ast and ("ast", old_play.get("ast"), new_play.get("ast")) not in diffs:
        diffs.append(("ast", old_ast, new_ast))

    return diffs

# ── DISCORD ───────────────────────────────────────────────────────────────────
def ordinal(n):
    return {1:"1st", 2:"2nd", 3:"3rd", 4:"4th"}.get(n, f"{n}th")

def dot_color(ctype):
    return {"removed": "🔴", "added": "🟢", "mixup": "🟡",
            "points": "🟣", "rebound": "🔵"}.get(ctype, "⚪")

def post_to_discord(game, play, old_play, correction_type, label, stat,
                    old_val, new_val, seconds_elapsed):
    period_str = ordinal(play.get("period", 0))
    clock      = play.get("clock", "").replace("PT","").replace("M","m ").replace("S","s")
    game_code  = game.get("gameCode", "?").replace("/", " vs ")
    play_num   = play.get("actionNumber", "?")
    player     = play.get("playerNameI", "Unknown")
    desc       = play.get("description", "")

    if stat == "ast":
        if correction_type == "mixup":
            old_name = old_play.get("assistPlayerNameInitial") or str(old_val) or "none"
            new_name = play.get("assistPlayerNameInitial") or str(new_val) or "none"
            change_line = f"❌ Taken from: **{old_name}**\n✅ Given to: **{new_name}**"
        elif correction_type == "removed":
            old_name = old_play.get("assistPlayerNameInitial") or str(old_val) or "none"
            change_line = f"❌ Removed from: **{old_name}**"
        elif correction_type == "added":
            new_name = play.get("assistPlayerNameInitial") or str(new_val) or "none"
            change_line = f"✅ Added to: **{new_name}**"
        else:
            change_line = f"{old_val} → {new_val}"
    else:
        change_line = f"{old_val} → {new_val}"

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
            "footer": {"text": f"NBA Correction Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}
        }]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[discord error] {e}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def run():
    init_db()
    print("🏀 NBA Correction Bot started. Polling every "
          f"{POLL_INTERVAL_SECONDS}s for live games...\n")

    snapshots = {}

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
                snapshots[game_id] = {pid: (p, time.time()) for pid, p in plays.items()}
                print(f"  Tracking new game: {game_code} ({len(plays)} plays)")
                continue

            old_snap = snapshots[game_id]

            for pid, new_play in plays.items():
                if pid not in old_snap:
                    old_snap[pid] = (new_play, time.time())
                    continue

                old_play, first_seen = old_snap[pid]
                diffs = diff_plays(old_play, new_play)

                for (stat, old_v, new_v) in diffs:
                    correction_key = f"{game_id}_{pid}_{stat}_{old_v}_{new_v}"

                    # Check database first
                    if already_reported(correction_key):
                        continue

                    old_player = old_play.get("assistPlayerNameInitial")
                    new_player = new_play.get("assistPlayerNameInitial")
                    ctype, label = classify_correction(stat, old_v, new_v,
                                                       old_player, new_player)
                    elapsed = time.time() - first_seen

                    # ── SAVE TO DATABASE FIRST, THEN POST ──
                    # This guarantees no duplicates even if bot crashes mid-post
                    saved = save_correction(
                        game_id, pid,
                        new_play.get("playerNameI", "?"),
                        stat, old_v, new_v,
                        new_play.get("description", ""),
                        new_play.get("period", 0),
                        new_play.get("clock", ""),
                        datetime.utcnow().isoformat(),
                        elapsed,
                        correction_key
                    )

                    if not saved:
                        # Already in database — skip posting
                        continue

                    print(f"  ✅ CORRECTION: {new_play.get('playerNameI','?')} "
                          f"— {label} ({game_code})")

                    post_to_discord(game, new_play, old_play, ctype, label,
                                    stat, old_v, new_v, elapsed)

                old_snap[pid] = (new_play, first_seen)

            snapshots[game_id] = old_snap

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run()
