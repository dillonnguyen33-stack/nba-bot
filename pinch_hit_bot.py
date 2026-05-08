"""
MLB Pinch Hit Alert Bot - v8.7 (smart result-position + clause stripping)
Changes from v8.1:
1. Player name regex loosened — no longer requires strict Title Case
2. PH/ph/Ph all accepted (case-insensitive matching throughout)
3. Added "on deck to pinch hit for" with both player names
4. Added "will ph for" / "will PH for" / "will be ph for" patterns
5. Added "on deck to ph for" pattern
"""

import os
import re
import time
import json
import threading
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ET_TZ                = ZoneInfo("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2
LINEUP_REFRESH_SECS  = 300

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
    "will ph for",
    "will be ph for",
    "on deck to ph",
    "ph for",
    "phing for",
]

REJECT_PHRASES = [
    # time references
    "last night", "yesterday",
    # non-MLB contexts
    "college", "university", "high school", "ncaa",
    "minor league", "minors", "triple-a", "double-a",
    "softball", "little league",
    # explicit past tense pinch hit
    "just pinch hit", "just pinch-hit",
    "just ph'd", "just phd",
    "pinch hit earlier", "pinch-hit earlier",
    "already pinch hit", "already pinch-hit",
    # passive constructions (already happened)
    "has been pinch hit", "has been pinch-hit",
    "was pinch hit", "was pinch-hit",
    "have been pinch hit", "have been pinch-hit",
    "got pinch hit", "got pinch-hit",
    # result followed by "after being pinch hit"
    "after being pinch hit", "after being pinch-hit",
    "after pinch hit", "after ph",
]

STREAM_RULES = [
    {"value": '"pinch hit for" -is:retweet lang:en',        "tag": "pinch_hit_for"},
    {"value": '"pinch-hit for" -is:retweet lang:en',        "tag": "pinch_hit_for_hyph"},
    {"value": '"on deck to pinch hit" -is:retweet lang:en', "tag": "on_deck"},
    {"value": '"slated to pinch hit" -is:retweet lang:en',  "tag": "slated"},
    {"value": '"will pinch hit" -is:retweet lang:en',       "tag": "will_ph"},
    {"value": '"will ph for" -is:retweet lang:en',          "tag": "will_ph_abbrev"},
    {"value": '"on deck to ph" -is:retweet lang:en',        "tag": "on_deck_ph_abbrev"},
]

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids      = set()
posted_alert_keys   = set()
last_reset_date     = None
player_alert_count  = {}
player_alert_time   = {}
daily_lineup_map    = {}
last_lineup_refresh = 0
_lineup_lock        = threading.Lock()

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

# ── DAILY LINEUP CHECK ────────────────────────────────────────────────────────
def _do_lineup_refresh():
    global daily_lineup_map, last_lineup_refresh
    print("[lineup] Refreshing today's MLB lineups...")
    new_map = {}

    try:
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
            boxscore = r.json().get("liveData", {}).get("boxscore", {})

            for side in ["home", "away"]:
                team_data   = boxscore.get("teams", {}).get(side, {})
                team_name   = team_data.get("team", {}).get("name", "")
                team_mapped = None
                for alias, mapped in TEAM_ALIASES.items():
                    if alias in team_name.lower():
                        team_mapped = mapped
                        break

                for pid, pdata in team_data.get("players", {}).items():
                    full_name = pdata.get("person", {}).get("fullName", "")
                    if not full_name:
                        continue
                    parts    = full_name.split()
                    full_low = full_name.lower()
                    team     = team_mapped or team_name
                    new_map[full_low] = team
                    if len(parts) >= 2:
                        new_map[parts[0].lower() + " " + parts[-1].lower()] = team
                        new_map["_last_" + parts[-1].lower()] = team

        except Exception as e:
            print(f"[lineup] Game {game_pk} error: {e}")

    with _lineup_lock:
        daily_lineup_map    = new_map
        last_lineup_refresh = time.time()
    print(f"[lineup] {len([k for k in new_map if not k.startswith('_last_')])} players loaded\n")

def start_lineup_refresh_thread():
    def loop():
        while True:
            now = time.time()
            with _lineup_lock:
                needs_refresh = (now - last_lineup_refresh >= LINEUP_REFRESH_SECS
                                 or not daily_lineup_map)
            if needs_refresh:
                try:
                    _do_lineup_refresh()
                except Exception as e:
                    print(f"[lineup] Refresh error: {e}")
            time.sleep(30)
    threading.Thread(target=loop, daemon=True).start()
    print("[lineup] Background refresh thread started (every 5 min)\n")

def is_todays_player(name):
    if not name:
        return False
    with _lineup_lock:
        if not daily_lineup_map:
            return False
        nl    = name.lower().strip()
        parts = nl.split()
        if nl in daily_lineup_map and " " in nl:
            return True
        if len(parts) >= 2:
            if parts[0] + " " + parts[-1] in daily_lineup_map:
                return True
        if len(parts) >= 2 and len(parts[-1]) >= 5:
            if "_last_" + parts[-1] in daily_lineup_map:
                return True
    return False

def lookup_player_team(name):
    if not name:
        return None
    with _lineup_lock:
        if not daily_lineup_map:
            return None
        nl    = name.lower().strip()
        parts = nl.split()
        if nl in daily_lineup_map:
            return daily_lineup_map[nl]
        if len(parts) >= 2:
            fl = parts[0] + " " + parts[-1]
            if fl in daily_lineup_map:
                return daily_lineup_map[fl]
    return None

def infer_team_from_text(text):
    tl = text.lower()
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    words = text.split()
    with _lineup_lock:
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

RESULT_WORDS = [
    "home run", "homerun", "homered", "homer",
    "singled", "doubled", "tripled",
    "struck out", "strikeout", "walked", "grounded out", "flied out", "lined out",
    "drove in", "rbi", "scored",
]

PINCH_HIT_PHRASES_LOWER = [
    "pinch hit for", "pinch-hit for",
    "pinch hitting for", "pinch-hitting for",
    "will pinch hit", "will ph for", "ph for", "phing for",
    "on deck to pinch hit", "slated to pinch hit",
    "will be ph for", "on deck to ph",
]

def result_comes_after_ph(text):
    """Reject if a result word (homered, singled, etc.) appears AFTER the ph phrase.
    This allows 'Ohtani who homered earlier will ph for Freeman' (valid)
    while blocking 'Ohtani ph for Freeman and homered' (already happened)."""
    tl = text.lower()
    ph_pos = -1
    for phrase in PINCH_HIT_PHRASES_LOWER:
        pos = tl.find(phrase)
        if pos != -1 and (ph_pos == -1 or pos < ph_pos):
            ph_pos = pos
    if ph_pos == -1:
        return False
    after_text = tl[ph_pos:]
    return any(word in after_text for word in RESULT_WORDS)

def is_question(text):
    """Reject tweets that are questions or speculation, not confirmations."""
    stripped = text.strip()
    # Ends with ? (possibly followed by emoji/spaces/punctuation)
    if re.search(r"\?[\s\W]*$", stripped):
        return True
    tl = stripped.lower()
    # Starts with a true question word (not will/is/are which appear in valid alerts)
    if re.match(r"(did|does|would|should|could|can)\b", tl):
        return True
    return False

def has_reject_phrase(text):
    tl = text.lower()
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return True, phrase
    return False, None

# Flexible name: 2+ words, any capitalization, allows hyphens and apostrophes
# Uses a possessive pattern to avoid swallowing function words
STOP_WORDS = r'(?:will|is|are|was|has|had|be|to|on|in|for|the|a|an|just|going|deck|slated|pinch|ph)'
NAME = r"(?!" + STOP_WORDS + r"\b)([A-Za-z][A-Za-z'\-]{2,}(?:\s(?!" + STOP_WORDS + r"\b)[A-Za-z][A-Za-z'\-]{2,})*)"


def preprocess_text(text):
    """Strip relative clauses that obscure player name structure."""
    # Handle ", who ... ," comma-enclosed relative clause
    text = re.sub(r",\s+who\s+[^,]+,", " ", text, flags=re.IGNORECASE)
    # Handle "who ..." without surrounding commas, up to next keyword
    text = re.sub(r",?\s+who\s+[^,]+?(?=\s+(?:will|is|are|has|ph\b|pinch|slated|on\s+deck))", "", text, flags=re.IGNORECASE)
    # Remove parentheticals like (2-for-3)
    text = re.sub(r"\([^)]+\)", "", text)
    # Clean up extra spaces
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().lstrip(",").strip()

def extract_players(text):
    clean = preprocess_text(strip_mentions(text))

    # All patterns use re.IGNORECASE — handles PH/ph/Ph and any name casing
    patterns = [
        # "Player A will be on deck to pinch hit for Player B"
        NAME + r'\s+will\s+be\s+on\s+deck\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        # "Player A will pinch hit for Player B"
        NAME + r'\s+will\s+pinch[- ]hit(?:ting)?\s+for\s+' + NAME,
        # "Player A is on deck to pinch hit for Player B"
        NAME + r'\s+(?:is\s+)?on\s+deck\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        # "Player A pinch hitting/hit for Player B"
        NAME + r'\s+(?:is\s+)?pinch[- ]hit(?:ting)?\s+for\s+' + NAME,
        # "Player A slated to pinch hit for Player B"
        NAME + r'\s+slated\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        # "Player A will ph for Player B" / "will PH for" / "will be ph for"
        NAME + r'\s+will\s+(?:be\s+)?ph\s+for\s+' + NAME,
        # "Player A on deck to ph for Player B"
        NAME + r'\s+(?:is\s+)?on\s+deck\s+to\s+ph\s+for\s+' + NAME,
        # "Player A ph/phing for Player B"
        NAME + r'\s+ph(?:ing)?\s+for\s+' + NAME,
        # passive: "Player A is getting pinch hit for by Player B"
        NAME + r'\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for\s+by\s+' + NAME,
        # "Player A left the game ... Player B will pinch"
        NAME + r'\s+(?:has\s+)?left\s+the\s+game[^.]*?' + NAME + r'\s+(?:will\s+)?pinch',
    ]

    for p in patterns:
        m = re.search(p, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    # Fallback: try matching single capitalized last names (e.g. "Ohtani ph for Freeman")
    single_name = r"([A-Z][A-Za-z'\-]+)"
    fallback_patterns = [
        single_name + r"\s+will\s+(?:be\s+)?ph\s+for\s+" + single_name,
        single_name + r"\s+(?:is\s+)?on\s+deck\s+to\s+ph\s+for\s+" + single_name,
        single_name + r"\s+(?:is\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+" + single_name,
        single_name + r"\s+will\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+will\s+be\s+on\s+deck\s+to\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+slated\s+to\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+ph(?:ing)?\s+for\s+" + single_name,
    ]
    for p in fallback_patterns:
        m = re.search(p, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    return None, None

# ── DISCORD ───────────────────────────────────────────────────────────────────
def _post_discord_now(payload):
    if not DISCORD_WEBHOOK_URL:
        print("[discord error] Webhook URL missing!")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[discord error] {e}")

def post_discord(payload):
    threading.Thread(target=_post_discord_now, args=(payload,), daemon=True).start()

def post_reporter_alert(handle, text, url, team, pinch_hitter, replaced):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed = {"embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
        "description": (
            f"**Verified beat reporter — pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
        ),
        "color": 0x00FF00,
        "footer": {"text": f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]}
    post_discord({"content": "@everyone 🔥 BEAT REPORTER", "embeds": embed["embeds"]})
    print(f"  🟢 Reporter: {team} — {summary}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed = {"embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
        "description": (
            f"**Twitter source — pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🌐 **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"💰 **BET THE UNDER NOW**"
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

    if is_question(text):
        print(f"  🚫 @{handle}: question/speculation — {text[:60]}")
        return

    if result_comes_after_ph(text):
        print(f"  🚫 @{handle}: result after ph (already happened) — {text[:60]}")
        return

    rejected, phrase = has_reject_phrase(text)
    if rejected:
        print(f"  🚫 @{handle}: '{phrase}' — {text[:60]}")
        return

    pinch_hitter, replaced = extract_players(text)
    if not pinch_hitter or not replaced:
        print(f"  🚫 @{handle}: need both names — ph={pinch_hitter} out={replaced}")
        return

    if not is_todays_player(pinch_hitter) or not is_todays_player(replaced):
        print(f"  🚫 @{handle}: '{pinch_hitter}' or '{replaced}' not in today's lineups")
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

    if is_reporter:
        post_reporter_alert(handle, text, url, team, pinch_hitter, replaced)
    else:
        post_general_alert(handle, text, url, team, pinch_hitter, replaced)

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
        print("[stream] 429 rate limit — waiting 5 min...")
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
                try:
                    data = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeDecodeError):
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

# ── REPORTER POLLER ───────────────────────────────────────────────────────────
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

def get_user_tweets(user_id, max_results=5):
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
    def loop():
        while True:
            if is_game_hours():
                for reporter in REPORTERS:
                    handle = reporter["handle"].lower()
                    uid    = user_ids.get(handle)
                    if not uid:
                        continue
                    for t in get_user_tweets(uid, max_results=5):
                        handle_tweet(t.get("id", ""), t.get("text", ""), handle)
            time.sleep(10)
    threading.Thread(target=loop, daemon=True).start()
    print("[reporters] Background poller started (every 10s)\n")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot v8.2 — phrase + capitalization fix")
    print("   Poll interval: 10s")
    print("   Lineup refresh: background thread only (never blocks alerts)")
    print("   Discord: fires in background (never blocks next tweet)")
    print("   Golden rule: both names required, both in today's games")
    print(f"   {len(REPORTERS)} reporters | game hours 12pm-1am ET\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return

    start_lineup_refresh_thread()
    time.sleep(5)

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
