"""
MLB Pinch Hit Alert Bot - v7
Key change: replaced static roster check with live daily lineup check.
Instead of checking 40-man roster, we check actual players in today's games.
This solves the callup problem — if they're in the lineup they're in the system.
Golden rule still applies: both player names required, both in today's lineups.
"""

import os
import re
import time
import json
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY")
ET_TZ                = ZoneInfo("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2
LINEUP_REFRESH_SECS  = 300   # refresh lineups every 5 minutes

# ── BEAT REPORTERS ────────────────────────────────────────────────────────────
REPORTERS = [
    {"handle": "JakeDRill",       "team": "Orioles"},
    {"handle": "masnRoch",        "team": "Orioles"},
    {"handle": "IanMBrowne",      "team": "Red Sox"},
    {"handle": "alexspeier",      "team": "Red Sox"},
    {"handle": "BryanHoch",       "team": "Yankees"},
    {"handle": "GJoyce9",         "team": "Yankees"},
    {"handle": "adamdberry",      "team": "Rays"},
    {"handle": "TBTimes_Rays",    "team": "Rays"},
    {"handle": "KeeganMatheson",  "team": "Blue Jays"},
    {"handle": "ShiDavidi",       "team": "Blue Jays"},
    {"handle": "scottmerkin",     "team": "White Sox"},
    {"handle": "JRFegan",         "team": "White Sox"},
    {"handle": "ZackMeisel",      "team": "Guardians"},
    {"handle": "beckjason",       "team": "Tigers"},
    {"handle": "CodyStavenhagen", "team": "Tigers"},
    {"handle": "alec_lewis",      "team": "Royals"},
    {"handle": "DanHayesMLB",     "team": "Twins"},
    {"handle": "dohyoungpark",    "team": "Twins"},
    {"handle": "brianmctaggart",  "team": "Astros"},
    {"handle": "Chandler_Rome",   "team": "Astros"},
    {"handle": "RhettBollinger",  "team": "Angels"},
    {"handle": "JeffFletcherOCR", "team": "Angels"},
    {"handle": "MartinJGallegos", "team": "Athletics"},
    {"handle": "DKramer_",        "team": "Mariners"},
    {"handle": "RyanDivish",      "team": "Mariners"},
    {"handle": "kennlandry",      "team": "Rangers"},
    {"handle": "Evan_P_Grant",    "team": "Rangers"},
    {"handle": "mlbbowman",       "team": "Braves"},
    {"handle": "DOBrienATL",      "team": "Braves"},
    {"handle": "AnthonyDiComo",   "team": "Mets"},
    {"handle": "TimBritton",      "team": "Mets"},
    {"handle": "ToddZolecki",     "team": "Phillies"},
    {"handle": "MattGelb",        "team": "Phillies"},
    {"handle": "CDeNicola13",     "team": "Marlins"},
    {"handle": "J_McPherson1126", "team": "Marlins"},
    {"handle": "JessicaCamerato", "team": "Nationals"},
    {"handle": "MarkZuckerman",   "team": "Nationals"},
    {"handle": "MLBastian",       "team": "Cubs"},
    {"handle": "sahadevsharma",   "team": "Cubs"},
    {"handle": "m_sheldon",       "team": "Reds"},
    {"handle": "AdamMcCalvy",     "team": "Brewers"},
    {"handle": "Todd_Rosiak",     "team": "Brewers"},
    {"handle": "justdelossantos", "team": "Pirates"},
    {"handle": "katiejwoo",       "team": "Cardinals"},
    {"handle": "JohnDenton555",   "team": "Cardinals"},
    {"handle": "SteveGilbertMLB", "team": "Diamondbacks"},
    {"handle": "nickpiecoro",     "team": "Diamondbacks"},
    {"handle": "harding_at_mlb",  "team": "Rockies"},
    {"handle": "psaundersdp",     "team": "Rockies"},
    {"handle": "juanctoribio",    "team": "Dodgers"},
    {"handle": "billplunkettocr", "team": "Dodgers"},
    {"handle": "AJCassavell",     "team": "Padres"},
    {"handle": "dennistlin",      "team": "Padres"},
    {"handle": "extrabaggs",      "team": "Giants"},
    {"handle": "mi_guardado",     "team": "Giants"},
]

REPORTER_HANDLES   = {r["handle"].lower() for r in REPORTERS}
REPORTER_BY_HANDLE = {r["handle"].lower(): r for r in REPORTERS}

TEAM_ALIASES = {
    "orioles": "Orioles", "baltimore": "Orioles",
    "red sox": "Red Sox", "boston": "Red Sox",
    "yankees": "Yankees", "new york yankees": "Yankees",
    "rays": "Rays", "tampa bay": "Rays",
    "blue jays": "Blue Jays", "toronto": "Blue Jays",
    "white sox": "White Sox", "chicago white sox": "White Sox",
    "guardians": "Guardians", "cleveland": "Guardians",
    "tigers": "Tigers", "detroit": "Tigers",
    "royals": "Royals", "kansas city": "Royals",
    "twins": "Twins", "minnesota": "Twins",
    "astros": "Astros", "houston": "Astros",
    "angels": "Angels", "los angeles angels": "Angels",
    "athletics": "Athletics", "oakland": "Athletics",
    "mariners": "Mariners", "seattle": "Mariners",
    "rangers": "Rangers", "texas": "Rangers",
    "braves": "Braves", "atlanta": "Braves",
    "marlins": "Marlins", "miami": "Marlins",
    "mets": "Mets", "new york mets": "Mets",
    "phillies": "Phillies", "philadelphia": "Phillies",
    "nationals": "Nationals", "washington": "Nationals",
    "cubs": "Cubs", "chicago cubs": "Cubs",
    "reds": "Reds", "cincinnati": "Reds",
    "brewers": "Brewers", "milwaukee": "Brewers",
    "pirates": "Pirates", "pittsburgh": "Pirates",
    "cardinals": "Cardinals", "st. louis": "Cardinals", "st louis": "Cardinals",
    "diamondbacks": "Diamondbacks", "arizona": "Diamondbacks", "dbacks": "Diamondbacks",
    "rockies": "Rockies", "colorado": "Rockies",
    "dodgers": "Dodgers", "los angeles dodgers": "Dodgers",
    "padres": "Padres", "san diego": "Padres",
    "giants": "Giants", "san francisco": "Giants",
}

CORE_PHRASES = [
    "pinch hit for",
    "pinch-hit for",
    "on deck to pinch hit",
    "slated to pinch hit",
    "will pinch hit",
    "pinch hitting for",
    "pinch-hitting for",
]

REJECT_PHRASES = [
    "home run", "homered",
    "singled", "doubled", "tripled",
    "drove in",
    "pinch-hit home run", "pinch hit home run",
    "pinch-hit single", "pinch hit single",
    "last night", "yesterday",
    "college", "university", "high school", "ncaa",
    "minor league", "minors", "triple-a", "double-a",
    "softball", "little league",
]

PROP_BOOKS = {
    "draftkings":  "DraftKings",
    "fanduel":     "FanDuel",
    "fliff":       "Fliff",
    "hardrockbet": "Hard Rock",
    "bet365":      "Bet365",
}

MLB_PROP_MARKETS = ["batter_hits", "batter_total_bases", "batter_rbis", "batter_home_runs"]

STREAM_RULES = [
    {"value": '"pinch hit for" -is:retweet lang:en',        "tag": "pinch_hit_for"},
    {"value": '"pinch-hit for" -is:retweet lang:en',        "tag": "pinch_hit_for_hyph"},
    {"value": '"on deck to pinch hit" -is:retweet lang:en', "tag": "on_deck"},
    {"value": '"slated to pinch hit" -is:retweet lang:en',  "tag": "slated"},
    {"value": '"will pinch hit" -is:retweet lang:en',       "tag": "will_ph"},
]

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids      = set()
posted_alert_keys   = set()
last_reset_date     = None
player_alert_count  = {}
player_alert_time   = {}

# Daily lineup cache — {full_name_lower: team_name}
daily_lineup_map    = {}
last_lineup_refresh = 0

TWITTER_HEADERS = {
    "Authorization": f"Bearer {TWITTER_BEARER_TOKEN}",
    "Content-Type":  "application/json",
}

# ── GAME HOURS / RESET ────────────────────────────────────────────────────────
def is_game_hours():
    hour = datetime.now(ET_TZ).hour
    return hour >= 12 or hour == 0

def maybe_reset_daily():
    global seen_tweet_ids, last_reset_date, posted_alert_keys
    global player_alert_count, player_alert_time, daily_lineup_map, last_lineup_refresh
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print("[reset] New day — clearing all state")
        seen_tweet_ids      = set()
        posted_alert_keys   = set()
        player_alert_count  = {}
        player_alert_time   = {}
        daily_lineup_map    = {}
        last_lineup_refresh = 0
        last_reset_date     = today

# ── PLAYER COOLDOWN ───────────────────────────────────────────────────────────
def is_player_on_cooldown(name):
    if not name:
        return False
    key = name.lower().split()[-1]
    now = time.time()
    if key in player_alert_time:
        if now - player_alert_time[key] > PLAYER_COOLDOWN_SEC:
            player_alert_count[key] = 0
    return player_alert_count.get(key, 0) >= PLAYER_MAX_ALERTS

def record_player_alert(name):
    if not name:
        return
    key = name.lower().split()[-1]
    player_alert_count[key] = player_alert_count.get(key, 0) + 1
    player_alert_time[key]  = time.time()

# ── DAILY LINEUP CHECK — replaces static roster ───────────────────────────────
def build_daily_lineup_map():
    """
    Pulls every player from today's MLB games (scheduled, pre-game, live).
    Includes starting lineups AND bench players.
    Refreshes every 5 minutes so callups appear quickly.
    """
    global daily_lineup_map, last_lineup_refresh
    now = time.time()
    if now - last_roster_refresh < 900 and player_team_map:  # 15min — catch in-game roster moves
        return

    print("[lineup] Refreshing today's MLB lineups...")
    new_map = {}

    try:
        # Get today's schedule
        today_str = datetime.now(ET_TZ).strftime("%Y-%m-%d")
        sched = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "date": today_str, "gameType": "R",
                    "hydrate": "roster,lineups"},
            timeout=10
        )
        sched.raise_for_status()
        game_pks = [
            g["gamePk"]
            for d in sched.json().get("dates", [])
            for g in d.get("games", [])
        ]
    except Exception as e:
        print(f"[lineup] Schedule error: {e}")
        return

    for game_pk in game_pks:
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
                timeout=10
            )
            r.raise_for_status()
            data     = r.json()
            boxscore = data.get("liveData", {}).get("boxscore", {})

            for side in ["home", "away"]:
                team_data   = boxscore.get("teams", {}).get(side, {})
                team_info   = team_data.get("team", {})
                team_name   = team_info.get("name", "")
                # Map team name to our alias
                team_mapped = None
                for alias, mapped in TEAM_ALIASES.items():
                    if alias in team_name.lower():
                        team_mapped = mapped
                        break

                players = team_data.get("players", {})
                for pid, pdata in players.items():
                    full_name = pdata.get("person", {}).get("fullName", "")
                    if not full_name:
                        continue
                    parts    = full_name.split()
                    full_low = full_name.lower()
                    new_map[full_low] = team_mapped or team_name
                    if len(parts) >= 2:
                        # first + last
                        new_map[parts[0].lower() + " " + parts[-1].lower()] = team_mapped or team_name
                        # last name only — stored separately for fuzzy fallback
                        new_map["_last_" + parts[-1].lower()] = team_mapped or team_name

            time.sleep(0.2)
        except Exception as e:
            print(f"[lineup] Game {game_pk} error: {e}")

    daily_lineup_map    = new_map
    last_lineup_refresh = now
    print(f"[lineup] {len([k for k in new_map if not k.startswith('_last_')])} players loaded from today's games\n")

def is_todays_player(name):
    """
    Check if player is in today's MLB games.
    First tries full name match, then first+last, then last name only as fallback.
    Last name only requires the name has at least 2 parts (first + last).
    """
    if not name or not daily_lineup_map:
        return False

    nl    = name.lower().strip()
    parts = nl.split()

    # Full name match
    if nl in daily_lineup_map and " " in nl:
        return True

    # First + last match (handles middle names)
    if len(parts) >= 2:
        first_last = parts[0] + " " + parts[-1]
        if first_last in daily_lineup_map:
            return True

    # Last name fallback — only if last name is 5+ chars (avoids common short names)
    if len(parts) >= 2 and len(parts[-1]) >= 5:
        last_key = "_last_" + parts[-1]
        if last_key in daily_lineup_map:
            return True

    return False

def lookup_player_team(name):
    if not name or not daily_lineup_map:
        return None
    nl    = name.lower().strip()
    parts = nl.split()

    if nl in daily_lineup_map:
        return daily_lineup_map[nl]
    if len(parts) >= 2:
        first_last = parts[0] + " " + parts[-1]
        if first_last in daily_lineup_map:
            return daily_lineup_map[first_last]
    return None

def infer_team_from_text(text):
    tl = text.lower()
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    words = text.split()
    for i in range(len(words) - 1):
        two = (words[i] + " " + words[i+1]).lower()
        if two in daily_lineup_map:
            return daily_lineup_map[two]
    return None

# ── DETECTION ─────────────────────────────────────────────────────────────────
def strip_mentions(text):
    return re.sub(r'@\w+', '', text)

def has_core_phrase(text):
    tl = text.lower()
    return any(phrase in tl for phrase in CORE_PHRASES)

def has_reject_phrase(text):
    tl = text.lower()
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return True, phrase
    return False, None

def extract_players(text):
    clean = strip_mentions(text)
    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?(?:bat|hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+slated\s+to\s+pinch[- ]hit\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for\s+by\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:has\s+)?left\s+the\s+game[^.]*?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch',
    ]
    for p in patterns_both:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), m.group(2).strip()
    return None, None

# ── ODDS ──────────────────────────────────────────────────────────────────────
def get_player_lines(player_name):
    if not ODDS_API_KEY or not player_name:
        return {}
    last_name = player_name.split()[-1].lower()
    results   = {}
    try:
        events = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/events",
            params={"apiKey": ODDS_API_KEY}, timeout=10
        ).json()
    except Exception as e:
        print(f"[odds error] {e}")
        return {}
    for event in events[:6]:
        event_id = event.get("id")
        for market in MLB_PROP_MARKETS:
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds",
                    params={"apiKey": ODDS_API_KEY, "markets": market,
                            "bookmakers": ",".join(PROP_BOOKS.keys()),
                            "oddsFormat": "american", "regions": "us,us2"},
                    timeout=10
                ).json()
            except Exception as e:
                print(f"[odds error] {e}")
                continue
            for bk in r.get("bookmakers", []):
                bname = PROP_BOOKS.get(bk.get("key", ""))
                if not bname:
                    continue
                for mkt in bk.get("markets", []):
                    for oc in mkt.get("outcomes", []):
                        if last_name not in oc.get("description", "").lower():
                            continue
                        if oc.get("name") != "Under":
                            continue
                        pt, pr = oc.get("point"), oc.get("price")
                        if pt is None or pr is None:
                            continue
                        label = market.replace("batter_", "").replace("_", " ").title()
                        key   = f"{bname}_{label}"
                        if key not in results:
                            results[key] = {"book": bname, "market": label,
                                            "line": pt, "under": pr}
    return results

def format_lines(lines_data):
    if not lines_data:
        return "📋 Lines: not found on tracked books"
    by_market = {}
    for d in lines_data.values():
        by_market.setdefault(d["market"], []).append(
            f"{d['book']}: u{d['line']} ({'+'if d['under']>0 else ''}{d['under']})"
        )
    out = ["📋 **BET UNDER — player lines:**"]
    for mkt, entries in by_market.items():
        out.append(f"**{mkt}:** " + " | ".join(entries))
    return "\n".join(out)

# ── DISCORD ───────────────────────────────────────────────────────────────────
def post_discord(payload):
    if not DISCORD_WEBHOOK_URL:
        print("[discord error] Webhook URL missing!")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[discord error] {e}")

def post_reporter_alert(handle, text, url, team, pinch_hitter, replaced, lines_data):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed = {"embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
        "description": (
            f"**Verified beat reporter — pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"{format_lines(lines_data)}\n\n"
            f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
        ),
        "color": 0x00FF00,
        "footer": {"text": f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]}
    post_discord({"content": "@everyone 🔥 BEAT REPORTER", "embeds": embed["embeds"]})
    print(f"  🟢 Reporter: {team} — {summary}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced, lines_data):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed = {"embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
        "description": (
            f"**Twitter source — pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🌐 **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"{format_lines(lines_data)}\n\n"
            f"💰 **BET THE UNDER ON ALL LINES NOW**"
        ),
        "color": 0xF1C40F,
        "footer": {"text": f"General Alert · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]}
    post_discord({"content": "@everyone", "embeds": embed["embeds"]})
    print(f"  🟡 General: {team} — {summary}")

# ── CORE: HANDLE SINGLE TWEET ─────────────────────────────────────────────────
def handle_tweet(tid, text, handle):
    maybe_reset_daily()

    if not is_game_hours():
        return

    if not tid or tid in seen_tweet_ids:
        return
    seen_tweet_ids.add(tid)

    if not has_core_phrase(text):
        return

    rejected, phrase = has_reject_phrase(text)
    if rejected:
        print(f"  🚫 @{handle}: '{phrase}' — {text[:60]}")
        return

    pinch_hitter, replaced = extract_players(text)

    # Golden rule — require both names
    if not pinch_hitter or not replaced:
        print(f"  🚫 @{handle}: need both names — ph={pinch_hitter} out={replaced}")
        return

    # Check both players are in today's actual MLB games
    build_daily_lineup_map()
    ph_in_lineup  = is_todays_player(pinch_hitter)
    rep_in_lineup = is_todays_player(replaced)

    if not ph_in_lineup or not rep_in_lineup:
        print(f"  🚫 @{handle}: '{pinch_hitter}'({ph_in_lineup}) or '{replaced}'({rep_in_lineup}) not in today's lineups")
        return

    if is_player_on_cooldown(pinch_hitter):
        print(f"  🔇 '{pinch_hitter}' on cooldown")
        return

    is_reporter = handle in REPORTER_HANDLES
    reporter    = REPORTER_BY_HANDLE.get(handle)
    team        = reporter["team"] if reporter else None
    if not team:
        team = lookup_player_team(pinch_hitter) or lookup_player_team(replaced)
    if not team:
        team = infer_team_from_text(text)
    if not team:
        team = "Unknown Team"

    url = f"https://twitter.com/{handle}/status/{tid}"
    if tid in posted_alert_keys:
        return
    posted_alert_keys.add(tid)

    print(f"  ✅ VALID: @{handle} ({'reporter' if is_reporter else 'general'}) "
          f"team={team} | {pinch_hitter} for {replaced}")
    print(f"     {text[:120]}")

    record_player_alert(pinch_hitter)
    lines_data = get_player_lines(pinch_hitter)

    if is_reporter:
        post_reporter_alert(handle, text, url, team, pinch_hitter, replaced, lines_data)
    else:
        post_general_alert(handle, text, url, team, pinch_hitter, replaced, lines_data)

# ── TWITTER STREAM ────────────────────────────────────────────────────────────
def get_stream_rules():
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS, timeout=10
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[stream rules error] {e}")
        return []

def delete_stream_rules(rule_ids):
    if not rule_ids:
        return
    try:
        requests.post(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS,
            json={"delete": {"ids": rule_ids}},
            timeout=10
        )
    except Exception as e:
        print(f"[stream rules delete error] {e}")

def add_stream_rules():
    try:
        r = requests.post(
            "https://api.twitter.com/2/tweets/search/stream/rules",
            headers=TWITTER_HEADERS,
            json={"add": STREAM_RULES},
            timeout=10
        )
        r.raise_for_status()
        data = r.json()
        print(f"[stream] Added {len(data.get('data', []))} filter rules")
        if data.get("errors"):
            print(f"[stream rule errors] {data['errors']}")
    except Exception as e:
        print(f"[stream rules add error] {e}")

def setup_stream_rules():
    existing = get_stream_rules()
    if existing:
        delete_stream_rules([r["id"] for r in existing])
        time.sleep(1)
    add_stream_rules()

def connect_stream():
    url = "https://api.twitter.com/2/tweets/search/stream"
    params = {
        "tweet.fields": "created_at,author_id,text",
        "expansions":   "author_id",
        "user.fields":  "username",
    }
    print("[stream] Connecting...")
    r = requests.get(url, headers=TWITTER_HEADERS, params=params,
                     stream=True, timeout=30)
    if r.status_code == 429:
        print(f"[stream] 429 rate limit — waiting 5 min...")
        time.sleep(300)
        return
    if r.status_code != 200:
        print(f"[stream error] HTTP {r.status_code}: {r.text[:200]}")
        return
    print("[stream] Connected! Listening for tweets...\n")
    for line in r.iter_lines():
        if line:
            yield line

def run_stream():
    reconnect_wait = 5
    while True:
        try:
            for raw_line in connect_stream():
                reconnect_wait = 5
                maybe_reset_daily()
                build_daily_lineup_map()
                try:
                    data = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    print(f"[stream error] unparseable payload: {raw_line[:100]}")
                    continue
                tweet_data = data.get("data", {})
                users      = {u["id"]: u["username"].lower()
                              for u in data.get("includes", {}).get("users", [])}
                handle_tweet(
                    tweet_data.get("id", ""),
                    tweet_data.get("text", ""),
                    users.get(tweet_data.get("author_id", ""), "unknown")
                )
        except requests.exceptions.ChunkedEncodingError:
            print(f"[stream] Dropped — reconnecting in {reconnect_wait}s...")
        except requests.exceptions.ConnectionError:
            print(f"[stream] Connection error — reconnecting in {reconnect_wait}s...")
        except Exception as e:
            print(f"[stream] Error: {e} — reconnecting in {reconnect_wait}s...")
        time.sleep(reconnect_wait)
        reconnect_wait = min(reconnect_wait * 2, 300)

# ── REPORTER POLLER (background thread) ──────────────────────────────────────
def get_user_ids_batch(handles):
    try:
        r = requests.get(
            "https://api.twitter.com/2/users/by",
            headers=TWITTER_HEADERS,
            params={"usernames": ",".join(handles)},
            timeout=10
        )
        r.raise_for_status()
        return {u["username"].lower(): u["id"] for u in r.json().get("data", [])}
    except Exception as e:
        print(f"[user id error] {e}")
        return {}

def get_user_tweets(user_id, max_results=3):
    try:
        r = requests.get(
            f"https://api.twitter.com/2/users/{user_id}/tweets",
            headers=TWITTER_HEADERS,
            params={"max_results": max_results, "tweet.fields": "created_at,text"},
            timeout=10
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        print(f"[user tweets error] {e}")
        return []

def poll_reporters_forever(user_ids):
    import threading
    def loop():
        while True:
            if is_game_hours():
                for reporter in REPORTERS:
                    handle = reporter["handle"].lower()
                    uid    = user_ids.get(handle)
                    if not uid:
                        continue
                    for t in get_user_tweets(uid, max_results=3):
                        handle_tweet(t.get("id", ""), t.get("text", ""), handle)
                    time.sleep(0.5)
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()
    print("[reporters] Background poller started (every 30s)\n")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("MLB Pinch Hit Bot starting")
    print(f"   Real-time filtered stream + reporter background poller")
    print(f"   {len(REPORTERS)} reporters | game hours 12pm-1am ET\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return

    build_daily_lineup_map()

    print("Looking up reporter user IDs...")
    handles  = [r["handle"] for r in REPORTERS]
    user_ids = {}
    for i in range(0, len(handles), 100):
        user_ids.update(get_user_ids_batch(handles[i:i+100]))
    print(f"Found {len(user_ids)} user IDs\n")

    setup_stream_rules()
    poll_reporters_forever(user_ids)
    run_stream()

if __name__ == "__main__":
    run()
