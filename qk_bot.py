"""
QK Bot - Kelly Criterion Calculator for NBA Player Props
Triggers only on @sub messages in #plays channel
Posts to #plays-qk with implied probability, EV%, and Kelly suggestion
"""

import os
import re
import requests
import discord
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
ODDS_API_KEY      = os.environ.get("ODDS_API_KEY")
PLAYS_CHANNEL_ID  = 1461266015433396400
QK_CHANNEL_ID     = 1488811439476183160

# Books used for implied probability averaging (vig-removed)
PROB_BOOKS  = {"draftkings", "fanduel", "bet365"}
ALL_BOOKS   = {"draftkings", "fanduel", "bet365", "fliff"}
BOOK_LABELS = {
    "draftkings": "DraftKings",
    "fanduel":    "FanDuel",
    "bet365":     "Bet365",
    "fliff":      "Fliff",
}

# ── NICKNAME DICTIONARY ───────────────────────────────────────────────────────
NICKNAMES = {
    "podz":   "podziemski",
    "bron":   "lebron",
    "dmitch": "mitchell",
}

NBA_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://www.nba.com/",
    "Origin":     "https://www.nba.com",
    "Accept":     "application/json",
}

STAT_MAP = {
    "a":        "assists",
    "ast":      "assists",
    "assists":  "assists",
    "p":        "points",
    "pts":      "points",
    "points":   "points",
    "r":        "rebounds",
    "reb":      "rebounds",
    "rebounds": "rebounds",
}

STAT_SHORT = {"assists": "AST", "points": "PTS", "rebounds": "REB"}
STAT_EMOJI = {"assists": "🎯", "points": "🏀", "rebounds": "💪"}

# ── PARSE PLAY ────────────────────────────────────────────────────────────────
def parse_play(message):
    text = message.content.strip()

    # Must start with @sub (mention or literal)
    if not (text.lower().startswith("@sub") or
            re.match(r'^<@[!&]?\d+>', text)):
        return None

    # Remove the @sub / mention
    text = re.sub(r'^<@[!&]?\d+>', '', text).strip()
    text = re.sub(r'^@?sub\s*', '', text, flags=re.IGNORECASE).strip()
    tl   = text.lower()

    # Units — support 1u, 1U, .5u, 0.5u, 1 u
    units_match = re.search(r'([\d.]+)\s*u\b', tl, re.IGNORECASE)
    units = float(units_match.group(1)) if units_match else 0.5

    # Line — o5.5, u5.5, o 5.5
    line_match = re.search(r'([ou])\s*([\d.]+)', tl)
    if not line_match:
        return None
    direction = "over" if line_match.group(1) == 'o' else "under"
    line      = float(line_match.group(2))

    # Stat type
    stat_type = None
    for key, val in STAT_MAP.items():
        if re.search(rf'\b{re.escape(key)}\b', tl):
            stat_type = val
            break
    if not stat_type:
        return None

    # Player name — everything before the o/u line
    player_raw = tl[:line_match.start()].strip()
    if not player_raw:
        return None

    # Apply nickname lookup
    player_search = NICKNAMES.get(player_raw, player_raw)

    return {
        "player_display": player_raw.title(),
        "player_search":  player_search,
        "line":           line,
        "direction":      direction,
        "stat_type":      stat_type,
        "units":          units,
    }

# ── NBA API ───────────────────────────────────────────────────────────────────
def get_live_games():
    url = "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
    try:
        r = requests.get(url, headers=NBA_HEADERS, timeout=10)
        r.raise_for_status()
        games = r.json().get("scoreboard", {}).get("games", [])
        return [g for g in games if g.get("gameStatus") == 2]
    except Exception as e:
        print(f"[scoreboard error] {e}")
        return []

def get_boxscore(game_id):
    url = f"https://cdn.nba.com/static/json/liveData/boxscore/boxscore_{game_id}.json"
    try:
        r = requests.get(url, headers=NBA_HEADERS, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[boxscore error] {e}")
        return {}

def find_player_stats(player_search, stat_type):
    """
    Returns:
      - None if no match found
      - None if multiple matches found (ambiguous)
      - dict if exactly one match found
    """
    games = get_live_games()
    if not games:
        return None

    name_lower = player_search.lower()
    matches    = []

    for game in games:
        game_id   = game["gameId"]
        data      = get_boxscore(game_id)
        game_data = data.get("game", {})

        period = game_data.get("period", 0)
        clock  = game_data.get("gameClock", "PT00M00.00S")
        match  = re.search(r'PT(\d+)M([\d.]+)S', clock)
        if match:
            time_left_period = int(match.group(1)) * 60 + float(match.group(2))
        else:
            time_left_period = 0

        total_secs     = max(0, 4 - period) * 720 + time_left_period
        mins_remaining = round(total_secs / 60, 1)

        for team_key in ["homeTeam", "awayTeam"]:
            for player in game_data.get(team_key, {}).get("players", []):
                full  = player.get("name", "").lower()
                first = player.get("firstName", "").lower()
                last  = player.get("familyName", "").lower()

                if (name_lower in full or
                    name_lower in last or
                    name_lower in first or
                    (len(name_lower) > 3 and name_lower in f"{first} {last}")):

                    stats   = player.get("statistics", {})
                    current = {
                        "assists":  stats.get("assists", 0),
                        "points":   stats.get("points", 0),
                        "rebounds": stats.get("reboundsTotal", 0),
                    }.get(stat_type, 0)

                    matches.append({
                        "name":           player.get("name", player_search.title()),
                        "official_stat":  current,
                        "corrected_stat": current + 1,
                        "stat_type":      stat_type,
                        "period":         period,
                        "mins_remaining": mins_remaining,
                        "game_code":      game.get("gameCode", "?").replace("/", " vs "),
                    })

    # Exactly 1 match = confident, 0 or 2+ = silent
    if len(matches) == 1:
        return matches[0]
    return None

# ── ODDS API ──────────────────────────────────────────────────────────────────
def american_to_implied(odds):
    if odds > 0:
        return 100 / (odds + 100)
    else:
        return abs(odds) / (abs(odds) + 100)

def remove_vig(imp_over, imp_under):
    total = imp_over + imp_under
    return imp_over / total if total > 0 else imp_over

def american_to_decimal(odds):
    return (odds / 100 + 1) if odds > 0 else (100 / abs(odds) + 1)

def get_all_book_odds(player_search, stat_type, line):
    if not ODDS_API_KEY:
        return {}, None

    market_map = {
        "assists":  "player_assists",
        "points":   "player_points",
        "rebounds": "player_rebounds",
    }
    market = market_map.get(stat_type)
    if not market:
        return {}, None

    try:
        events = requests.get(
            "https://api.the-odds-api.com/v4/sports/basketball_nba/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=10
        ).json()
    except Exception as e:
        print(f"[odds events error] {e}")
        return {}, None

    book_over  = {}
    book_under = {}

    for event in events[:8]:
        event_id = event.get("id")
        try:
            data = requests.get(
                f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds",
                params={
                    "apiKey":     ODDS_API_KEY,
                    "markets":    market,
                    "bookmakers": ",".join(ALL_BOOKS),
                    "oddsFormat": "american",
                    "regions":    "us,us2,uk,eu",
                },
                timeout=10
            ).json()
        except:
            continue

        for bookmaker in data.get("bookmakers", []):
            bkey = bookmaker.get("key", "")
            if bkey not in ALL_BOOKS:
                continue
            for mkt in bookmaker.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    desc  = outcome.get("description", "").lower()
                    pt    = outcome.get("point", 0)
                    price = outcome.get("price")
                    name  = outcome.get("name", "")

                    if player_search.lower() not in desc:
                        continue
                    if abs(pt - line) > 0.26:
                        continue
                    if price is None:
                        continue

                    if name == "Over":
                        book_over[bkey]  = price
                    elif name == "Under":
                        book_under[bkey] = price

    if not book_over:
        return {}, None

    # Vig-removed avg from FD, DK, Bet365 only
    true_probs = []
    for bkey in PROB_BOOKS:
        if bkey in book_over and bkey in book_under:
            true_probs.append(remove_vig(
                american_to_implied(book_over[bkey]),
                american_to_implied(book_under[bkey])
            ))
        elif bkey in book_over:
            true_probs.append(american_to_implied(book_over[bkey]))

    avg_prob = sum(true_probs) / len(true_probs) if true_probs else None
    return book_over, avg_prob

# ── CORRECTION ADJUSTMENT ─────────────────────────────────────────────────────
def adjust_prob_for_correction(base_prob, line, corrected_stat):
    if base_prob is None:
        return None
    needed_corrected = line - corrected_stat
    if needed_corrected <= 0:
        return 0.97
    needed_official = needed_corrected + 1
    if needed_official <= 0:
        return base_prob
    ease_ratio = needed_official / max(needed_corrected, 0.5)
    return round(min(base_prob * ease_ratio, 0.96), 4)

# ── KELLY & EV ────────────────────────────────────────────────────────────────
def kelly_criterion(prob, best_odds):
    decimal    = american_to_decimal(best_odds)
    b          = decimal - 1
    half_kelly = ((b * prob - (1 - prob)) / b) / 2
    units      = round(half_kelly * 5 * 2) / 2
    units      = max(0.0, min(units, 5.0))
    return {
        "half_kelly_pct":  round(half_kelly * 100, 1),
        "suggested_units": units,
    }

def calculate_ev(prob, odds):
    return round(((prob * american_to_decimal(odds)) - 1) * 100, 1)

# ── FORMAT OUTPUT ─────────────────────────────────────────────────────────────
def format_qk_message(play, player_data, book_odds, adj_prob, kelly_data):
    stat_short = STAT_SHORT.get(play["stat_type"], "")
    stat_emoji = STAT_EMOJI.get(play["stat_type"], "📊")

    # Probability line
    if adj_prob is None:
        prob_line = "⚪ Hit probability: N/A"
        ev_line   = ""
    else:
        pct = round(adj_prob * 100)
        indicator = "🟢" if pct >= 70 else ("🟡" if pct >= 55 else "🔴")
        prob_line = f"{indicator} Hit probability: **{pct}%** _(vig-removed, corrected)_"
        best_odds = max(book_odds.values()) if book_odds else -110
        ev        = calculate_ev(adj_prob, best_odds)
        ev_line   = f"\n{'📈' if ev > 0 else '📉'} EV%: **{'+' if ev > 0 else ''}{ev}%**"

    # Lines per book
    lines_rows = []
    for bkey in ["draftkings", "fanduel", "bet365", "fliff"]:
        if bkey in book_odds:
            o       = book_odds[bkey]
            display = f"+{o}" if o > 0 else str(o)
            tag     = " _(prob)_" if bkey in PROB_BOOKS else ""
            lines_rows.append(f"  {BOOK_LABELS[bkey]}: **{display}**{tag}")

    lines_section = (
        f"\n📋 Lines (over {play['line']} {stat_short}):\n" + "\n".join(lines_rows)
        if lines_rows else "\n📋 Lines: not found on tracked books"
    )

    # Kelly section
    if adj_prob and kelly_data:
        kelly_section = (
            f"\n📐 Half Kelly: **{kelly_data['half_kelly_pct']}%** of bankroll"
            f"\n💰 QK Suggest: **{kelly_data['suggested_units']}u**"
            f"\n📝 Your call: **{play['units']}u**"
        )
    else:
        kelly_section = f"\n📝 Your call: **{play['units']}u** _(Kelly N/A — no odds found)_"

    return (
        f"{stat_emoji} **{player_data['name']}** — "
        f"{play['direction'].upper()} {play['line']} {stat_short}\n"
        f"```"
        f"Official:   {player_data['official_stat']} {stat_short}\n"
        f"Corrected:  {player_data['corrected_stat']} {stat_short} (+1 pending)\n"
        f"Needed:     {max(0, play['line'] - player_data['corrected_stat'])} more\n"
        f"Time left:  {player_data['mins_remaining']} min  |  "
        f"Period: {player_data['period']}\n"
        f"Game:       {player_data['game_code']}"
        f"```\n"
        f"{prob_line}"
        f"{ev_line if adj_prob else ''}"
        f"{lines_section}"
        f"{kelly_section}\n"
        f"-# QK Bot · {datetime.utcnow().strftime('%H:%M UTC')}"
    )

# ── DISCORD BOT ───────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f"✅ QK Bot online as {client.user}")

@client.event
async def on_message(message):
    if message.author.bot:
        return
    if message.channel.id != PLAYS_CHANNEL_ID:
        return

    play = parse_play(message)
    if not play:
        return  # not a valid @sub play — silent

    # Find player — silent if 0 or 2+ matches
    player_data = find_player_stats(play["player_search"], play["stat_type"])
    if not player_data:
        return  # ambiguous or not found — silent

    qk_channel = client.get_channel(QK_CHANNEL_ID)
    if not qk_channel:
        print("[error] QK channel not found")
        return

    # Get odds
    book_odds, base_prob = get_all_book_odds(
        play["player_search"], play["stat_type"], play["line"]
    )

    # Adjust for +1 correction
    adj_prob = adjust_prob_for_correction(base_prob, play["line"], player_data["corrected_stat"])

    # Kelly
    kelly_data = None
    if adj_prob and book_odds:
        kelly_data = kelly_criterion(adj_prob, max(book_odds.values()))

    output = format_qk_message(play, player_data, book_odds, adj_prob, kelly_data)
    await qk_channel.send(output)

if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN)
