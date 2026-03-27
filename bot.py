"""
NBA Stat Correction Discord Bot
Monitors live NBA play-by-play for stat corrections and posts them to Discord.
"""

import time
import json
import sqlite3
import requests
import os
from datetime import datetime
from difflib import SequenceMatcher

# ── CONFIG ──────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "https://discordapp.com/api/webhooks/1486803328766574753/lVrHREVTqTkWiKl0rL1LKx8RuGZkJdD3IE1ZXXZ1YQi9z77IEzIeesP_GKLLn5r1lxgo")
POLL_INTERVAL_SECONDS = 30   # how often to check for corrections
DB_PATH = "corrections.db"

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
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id     TEXT,
            play_id     TEXT,
            player      TEXT,
            stat        TEXT,
            old_value   TEXT,
            new_value   TEXT,
            description TEXT,
            period      INTEGER,
            clock       TEXT,
            detected_at TEXT,
            seconds_to_correct REAL
        )
    """)
    conn.commit()
    conn.close()

def save_correction(game_id, play_id, player, stat, old_val, new_val,
                    description, period, clock, detected_at, seconds_to_correct):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO corrections
        (game_id, play_id, player, stat, old_value, new_value, description,
         period, clock, detected_at, seconds_to_correct)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (game_id, play_id, player, stat, str(old_val), str(new_val),
          description, period, clock, detected_at, seconds_to_correct))
    conn.commit()
    conn.close()

# ── NBA API ───────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://www.nba.com/",
    "Origin":     "https://www.nba.com",
    "Accept":     "application/json",
}

def get_live_scoreboard():
    """Fetch today's live games from the NBA CDN."""
    url = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        games = data.get("scoreboard", {}).get("games", [])
        live = [g for g in games if g.get("gameStatus") == 2]  # 2 = in progress
        return live
    except Exception as e:
        print(f"[scoreboard error] {e}")
        return []

def get_play_by_play(game_id):
    """Fetch full play-by-play for a game."""
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
    """Return (correction_type, label) for a detected change."""
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
    """Return list of (stat, old_val, new_val) for changed stats."""
    diffs = []
    for stat in WATCH_STATS:
        old_v = old_play.get(stat)
        new_v = new_play.get(stat)
        if old_v != new_v:
            diffs.append((stat, old_v, new_v))

    # Also check assist player name change even if stat count stays the same
    old_ast = old_play.get("assistPersonId")
    new_ast = new_play.get("assistPersonId")
    if old_ast != new_ast and ("ast", old_play.get("ast"), new_play.get("ast")) not in diffs:
        diffs.append(("ast", old_ast, new_ast))

    return diffs

# ── DISCORD ───────────────────────────────────────────────────────────────────
def ordinal(n):
    return {1:"1st",2:"2nd",3:"3rd",4:"4th"}.get(n, f"{n}th")

def dot_color(ctype):
    return {"removed": "🔴", "added": "🟢", "mixup": "🟡",
            "points": "🟣", "rebound": "🔵"}.get(ctype, "⚪")

def post_to_discord(game, play, correction_type, label, stat,
                    old_val, new_val, seconds_elapsed):
    """Post a formatted correction embed to Discord via webhook."""
    if DISCORD_WEBHOOK_URL == "YOUR_WEBHOOK_URL_HERE":
        print("[discord] No webhook set — printing to console instead.")
        print(f"  {dot_color(correction_type)} {play.get('playerNameI','?')} — {label}")
        print(f"  {play.get('description','')}")
        print(f"  {ordinal(play.get('period',0))} {play.get('clock','')} · "
              f"{game.get('gameCode','?')} · play #{play.get('actionNumber','?')}")
        if stat == "ast":
            old_name = play.get("assistPlayerNameInitial", "none") if new_val else "none"
            new_name = "none" if not new_val else play.get("assistPlayerNameInitial","?")
            print(f"  {old_name} → {new_name}")
        print(f"  ⏱ corrected {int(seconds_elapsed)}s after recorded\n")
        return

    period_str = ordinal(play.get("period", 0))
    clock      = play.get("clock", "").replace("PT","").replace("M","m ").replace("S","s")
    game_code  = game.get("gameCode", "?").replace("/", " vs ")
    play_num   = play.get("actionNumber", "?")
    player     = play.get("playerNameI", "Unknown")
    desc       = play.get("description", "")

    if stat == "ast":
        old_ast = old_val or "none"
        new_ast = new_val or "none"
        change_line = f"{old_ast} → {new_ast}"
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

    # snapshot[game_id][action_number] = (play_dict, first_seen_timestamp)
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
                # First time seeing this game — just store baseline, no diffs
                snapshots[game_id] = {pid: (p, time.time()) for pid, p in plays.items()}
                print(f"  Tracking new game: {game_code} ({len(plays)} plays)")
                continue

            old_snap = snapshots[game_id]

            for pid, new_play in plays.items():
                if pid not in old_snap:
                    # Brand new play — record it with timestamp
                    old_snap[pid] = (new_play, time.time())
                    continue

                old_play, first_seen = old_snap[pid]
                diffs = diff_plays(old_play, new_play)

                for (stat, old_v, new_v) in diffs:
                    old_player = old_play.get("assistPlayerNameInitial")
                    new_player = new_play.get("assistPlayerNameInitial")
                    ctype, label = classify_correction(stat, old_v, new_v,
                                                       old_player, new_player)
                    elapsed = time.time() - first_seen

                    print(f"  ✅ CORRECTION: {new_play.get('playerNameI','?')} "
                          f"— {label} ({game_code})")

                    post_to_discord(game, new_play, ctype, label,
                                    stat, old_v, new_v, elapsed)

                    save_correction(
                        game_id, pid,
                        new_play.get("playerNameI", "?"),
                        stat, old_v, new_v,
                        new_play.get("description", ""),
                        new_play.get("period", 0),
                        new_play.get("clock", ""),
                        datetime.utcnow().isoformat(),
                        elapsed
                    )

                # Update snapshot to latest version of this play
                old_snap[pid] = (new_play, first_seen)

            snapshots[game_id] = old_snap

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    run()
