"""
MLB Pinch Hit Alert Bot - v10.0
Changes from v9.0:
1. Full 26-man roster prefetch at startup — bench players now recognized
2. Last name match threshold lowered to 3+ chars (was 5+)
3. ASCII normalization — Latin players with accents now match (Acuña → acuna)
4. Expanded phrase list — "sent up to hit for", "batting for", "up to hit for", etc.
5. Claude AI clarifier — fires in background thread, posts follow-up reply with verdict
"""

import os
import re
import time
import json
import threading
import unicodedata
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
DISCORD_CHANNEL_ID   = os.environ.get("DISCORD_CHANNEL_ID")
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
ET_TZ                = ZoneInfo("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2
LINEUP_REFRESH_SECS  = 300

# ── ASCII NORMALIZATION ───────────────────────────────────────────────────────
def normalize(text):
    """Strip accents and lowercase — Acuña → acuna, Báez → baez"""
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()

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

# ── PHRASES ───────────────────────────────────────────────────────────────────
CORE_PHRASES = [
    # Original
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
    # New expanded phrases
    "sent up to hit for",
    "sent up for",
    "hitting in place of",
    "batting for",
    "up to hit for",
    "in to hit for",
    "called upon to hit for",
    "coming up to hit for",
]

REJECT_PHRASES = [
    "last night", "yesterday",
    "college", "university", "high school", "ncaa",
    "minor league", "minors", "triple-a", "double-a",
    "softball", "little league",
    "just pinch hit", "just pinch-hit",
    "just ph'd", "just phd",
    "pinch hit earlier", "pinch-hit earlier",
    "already pinch hit", "already pinch-hit",
    "has been pinch hit", "has been pinch-hit",
    "was pinch hit", "was pinch-hit",
    "have been pinch hit", "have been pinch-hit",
    "got pinch hit", "got pinch-hit",
    "after being pinch hit", "after being pinch-hit",
    "after pinch hit", "after ph",
    "pinched",
]

STREAM_RULES = [
    {"value": '"pinch hit for" -is:retweet lang:en',          "tag": "pinch_hit_for"},
    {"value": '"pinch-hit for" -is:retweet lang:en',          "tag": "pinch_hit_for_hyph"},
    {"value": '"on deck to pinch hit" -is:retweet lang:en',   "tag": "on_deck"},
    {"value": '"slated to pinch hit" -is:retweet lang:en',    "tag": "slated"},
    {"value": '"will pinch hit" -is:retweet lang:en',         "tag": "will_ph"},
    {"value": '"will ph for" -is:retweet lang:en',            "tag": "will_ph_abbrev"},
    {"value": '"on deck to ph" -is:retweet lang:en',          "tag": "on_deck_ph_abbrev"},
    {"value": '"sent up to hit for" -is:retweet lang:en',     "tag": "sent_up"},
    {"value": '"hitting in place of" -is:retweet lang:en',    "tag": "in_place_of"},
    {"value": '"sent up for" -is:retweet lang:en',            "tag": "sent_up_short"},
    {"value": '"called upon to hit for" -is:retweet lang:en', "tag": "called_upon"},
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
    key = normalize(name).split()[-1]
    now = time.time()
    if key in player_alert_time:
        if now - player_alert_time[key] > PLAYER_COOLDOWN_SEC:
            player_alert_count[key] = 0
    return player_alert_count.get(key, 0) >= PLAYER_MAX_ALERTS

def record_player_alert(name):
    if not name:
        return
    key = normalize(name).split()[-1]
    player_alert_count[key] = player_alert_count.get(key, 0) + 1
    player_alert_time[key]  = time.time()

# ── ROSTER / LINEUP MAP ───────────────────────────────────────────────────────
def _add_player_to_map(target_map, full_name, team_name):
    """Normalize and add all name variants for a player into the map."""
    if not full_name:
        return
    norm_full = normalize(full_name)
    parts     = norm_full.split()
    target_map[norm_full] = team_name
    if len(parts) >= 2:
        target_map[parts[0] + " " + parts[-1]] = team_name
        # Last name only — lowered threshold to 3+ chars (was 5+)
        if len(parts[-1]) >= 3:
            target_map["_last_" + parts[-1]] = team_name

def prefetch_full_rosters():
    """Pull active 26-man rosters for all 30 teams at startup — runs once in background."""
    def _run():
        try:
            teams_r = requests.get(
                "https://statsapi.mlb.com/api/v1/teams",
                params={"sportId": 1},
                timeout=10
            )
            teams_r.raise_for_status()
            team_ids = [t["id"] for t in teams_r.json().get("teams", [])]
        except Exception as e:
            print(f"[roster] Teams fetch error: {e}")
            return

        new_entries = {}
        for tid in team_ids:
            try:
                r = requests.get(
                    f"https://statsapi.mlb.com/api/v1/teams/{tid}/roster",
                    params={"rosterType": "active"},
                    timeout=10
                )
                r.raise_for_status()
                for p in r.json().get("roster", []):
                    full      = p.get("person", {}).get("fullName", "")
                    team_name = p.get("team", {}).get("name", "Unknown")
                    _add_player_to_map(new_entries, full, team_name)
            except Exception as e:
                print(f"[roster] Team {tid} error: {e}")

        with _lineup_lock:
            daily_lineup_map.update(new_entries)
        print(f"[roster] Full rosters loaded — {len(new_entries)} entries added\n")

    threading.Thread(target=_run, daemon=True).start()

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
                team_data = boxscore.get("teams", {}).get(side, {})
                team_name = team_data.get("team", {}).get("name", "")
                team_mapped = None
                for alias, mapped in TEAM_ALIASES.items():
                    if alias in team_name.lower():
                        team_mapped = mapped
                        break

                for pid, pdata in team_data.get("players", {}).items():
                    full = pdata.get("person", {}).get("fullName", "")
                    team = team_mapped or team_name
                    _add_player_to_map(new_map, full, team)

        except Exception as e:
            print(f"[lineup] Game {game_pk} error: {e}")

    with _lineup_lock:
        daily_lineup_map.update(new_map)
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
        nl    = normalize(name)
        parts = nl.split()
        if nl in daily_lineup_map and " " in nl:
            return True
        if len(parts) >= 2:
            if parts[0] + " " + parts[-1] in daily_lineup_map:
                return True
        # Last name only — 3+ chars
        if len(parts) >= 1 and len(parts[-1]) >= 3:
            if "_last_" + parts[-1] in daily_lineup_map:
                return True
    return False

def lookup_player_team(name):
    if not name:
        return None
    with _lineup_lock:
        if not daily_lineup_map:
            return None
        nl    = normalize(name)
        parts = nl.split()
        if nl in daily_lineup_map:
            return daily_lineup_map[nl]
        if len(parts) >= 2:
            fl = parts[0] + " " + parts[-1]
            if fl in daily_lineup_map:
                return daily_lineup_map[fl]
        if len(parts) >= 1 and len(parts[-1]) >= 3:
            key = "_last_" + parts[-1]
            if key in daily_lineup_map:
                return daily_lineup_map[key]
    return None

def infer_team_from_text(text):
    tl = normalize(text)
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    words = text.split()
    with _lineup_lock:
        for i in range(len(words) - 1):
            two = normalize(words[i] + " " + words[i+1])
            if two in daily_lineup_map:
                return daily_lineup_map[two]
    return None

# ── DETECTION ─────────────────────────────────────────────────────────────────
def strip_mentions(text):
    return re.sub(r'@\w+', '', text)

def has_core_phrase(text):
    tl = normalize(text)
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
    "sent up to hit for", "sent up for",
    "hitting in place of", "batting for",
    "up to hit for", "in to hit for",
    "called upon to hit for", "coming up to hit for",
]

def result_comes_after_ph(text):
    tl = normalize(text)
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
    stripped = text.strip()
    if re.search(r"\?[\s\W]*$", stripped):
        return True
    tl = stripped.lower()
    if re.match(r"(did|does|would|should|could|can)\b", tl):
        return True
    return False

def has_reject_phrase(text):
    tl = normalize(text)
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return True, phrase
    return False, None

STOP_WORDS = r'(?:will|is|are|was|has|had|be|to|on|in|for|the|a|an|just|going|deck|slated|pinch|ph)'
NAME = r"(?!" + STOP_WORDS + r"\b)([A-Za-z][A-Za-z'\-]{2,}(?:\s(?!" + STOP_WORDS + r"\b)[A-Za-z][A-Za-z'\-]{2,})*)"

def preprocess_text(text):
    text = re.sub(r",\s+who\s+[^,]+,", " ", text, flags=re.IGNORECASE)
    text = re.sub(r",?\s+who\s+[^,]+?(?=\s+(?:will|is|are|has|ph\b|pinch|slated|on\s+deck))", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\([^)]+\)", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip().lstrip(",").strip()

def extract_players(text):
    clean = preprocess_text(strip_mentions(text))

    patterns = [
        NAME + r'\s+will\s+be\s+on\s+deck\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        NAME + r'\s+will\s+pinch[- ]hit(?:ting)?\s+for\s+' + NAME,
        NAME + r'\s+(?:is\s+)?on\s+deck\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        NAME + r'\s+(?:is\s+)?pinch[- ]hit(?:ting)?\s+for\s+' + NAME,
        NAME + r'\s+slated\s+to\s+pinch[- ]hit\s+for\s+' + NAME,
        NAME + r'\s+will\s+(?:be\s+)?ph\s+for\s+' + NAME,
        NAME + r'\s+(?:is\s+)?on\s+deck\s+to\s+ph\s+for\s+' + NAME,
        NAME + r'\s+ph(?:ing)?\s+for\s+' + NAME,
        NAME + r'\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for\s+by\s+' + NAME,
        NAME + r'\s+(?:has\s+)?left\s+the\s+game[^.]*?' + NAME + r'\s+(?:will\s+)?pinch',
        # New phrase patterns
        NAME + r'\s+sent\s+up\s+(?:to\s+hit\s+)?for\s+' + NAME,
        NAME + r'\s+hitting\s+in\s+place\s+of\s+' + NAME,
        NAME + r'\s+batting\s+for\s+' + NAME,
        NAME + r'\s+(?:is\s+)?up\s+to\s+hit\s+for\s+' + NAME,
        NAME + r'\s+in\s+to\s+hit\s+for\s+' + NAME,
        NAME + r'\s+called\s+upon\s+to\s+hit\s+for\s+' + NAME,
        NAME + r'\s+coming\s+up\s+to\s+hit\s+for\s+' + NAME,
    ]

    for p in patterns:
        m = re.search(p, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    single_name = r"([A-Z][A-Za-z'\-]+)"
    fallback_patterns = [
        single_name + r"\s+will\s+(?:be\s+)?ph\s+for\s+" + single_name,
        single_name + r"\s+(?:is\s+)?on\s+deck\s+to\s+ph\s+for\s+" + single_name,
        single_name + r"\s+(?:is\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+" + single_name,
        single_name + r"\s+will\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+will\s+be\s+on\s+deck\s+to\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+slated\s+to\s+pinch[- ]hit\s+for\s+" + single_name,
        single_name + r"\s+ph(?:ing)?\s+for\s+" + single_name,
        single_name + r"\s+sent\s+up\s+(?:to\s+hit\s+)?for\s+" + single_name,
        single_name + r"\s+hitting\s+in\s+place\s+of\s+" + single_name,
        single_name + r"\s+batting\s+for\s+" + single_name,
        single_name + r"\s+(?:is\s+)?up\s+to\s+hit\s+for\s+" + single_name,
        single_name + r"\s+in\s+to\s+hit\s+for\s+" + single_name,
        single_name + r"\s+called\s+upon\s+to\s+hit\s+for\s+" + single_name,
        single_name + r"\s+coming\s+up\s+to\s+hit\s+for\s+" + single_name,
    ]
    for p in fallback_patterns:
        m = re.search(p, clean, re.IGNORECASE)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    return None, None

# ── CLAUDE AI CLARIFIER ───────────────────────────────────────────────────────
CLAUDE_SYSTEM_PROMPT = """You are an MLB pinch hit alert classifier. Your job is to determine if a tweet is reporting that a pinch hitter is ABOUT TO bat — meaning the at-bat has NOT happened yet and this is a pre-event alert.

Answer only YES or NO.

Rules:
- YES if the tweet clearly states a player will/is about to pinch hit for another player, before the at-bat occurs
- NO if the at-bat has already happened (past tense results like singled, homered, struck out)
- NO if it is a question or speculation
- NO if it references a historical game (last night, yesterday, last week)
- NO if it is about minor leagues, college, or non-MLB baseball
- NO if the word "pinched" appears (substitution already made)

Examples:
Tweet: "Mateo will pinch hit for Refsnyder" → YES
Tweet: "Sending up Acuna for Albies in the 8th" → YES
Tweet: "Jones sent up to hit for Smith" → YES
Tweet: "Shaw pinched for Hoerner in the 7th" → NO
Tweet: "Did they just pinch hit there?" → NO
Tweet: "Mateo pinch hit for Refsnyder and singled to left" → NO
Tweet: "Remember when Ortiz pinch hit back in 2004?" → NO
Tweet: "Jones will be hitting in place of Smith" → YES
Tweet: "batting for the cycle attempt here" → NO
Tweet: "Johnson batting for Williams" → YES
Tweet: "Dylan Beavers is on deck to pinch-hit for Tyler O'Neill." → YES
Tweet: "Felix Reyes will pinch hit for Bryce Harper in the bottom of the first inning." → YES
Tweet: "Tyler O'Neill draws a walk, and now Samuel Basallo will pinch-hit for Coby Mayo." → YES
Tweet: "Melendez is hitting over .300 and always gets pinch hit for, Baty is hitting .200 and continues to get at bats against lefties" → NO
Tweet: "Of all people, Alek Thomas launched a game-tying, pinch-hit two-run homer in the eighth inning of Game 4 of the 2023 NLCS off Craig Kimbrel" → NO
Tweet: "CHANDLER SIMPSON PINCH HIT 2 RUN RBI SINGLE." → NO
Tweet: "Wtf did they just have Felix Reyes PH for Bryce Harper?" → NO
Tweet: "Josh Bell is pinch hitting for Matt Wallner, who was hit by a pitch when he batted in the sixth inning." → YES
Tweet: "Austin Slater is out on deck to pinch hit for MJ Melendez." → YES

Answer only YES or NO. Nothing else."""

def claude_clarify(tweet_text, pinch_hitter, replaced, callback):
    """
    Calls Claude API in background to verify the tweet is a valid pre-event alert.
    Calls callback(is_valid: bool, reasoning: str) when done.
    Never blocks the main alert path.
    """
    if not ANTHROPIC_API_KEY:
        callback(True, "No API key — skipping AI check")
        return

    def _run():
        try:
            prompt = f'Tweet: "{tweet_text}"\nPinch hitter detected: {pinch_hitter}\nReplacing: {replaced}'
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 10,
                    "system":     CLAUDE_SYSTEM_PROMPT,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=15
            )
            r.raise_for_status()
            answer = r.json()["content"][0]["text"].strip().upper()
            is_valid = answer.startswith("YES")
            callback(is_valid, answer)
        except Exception as e:
            print(f"[claude] Error: {e}")
            callback(True, f"AI check failed: {e}")

    threading.Thread(target=_run, daemon=True).start()

# ── DISCORD ───────────────────────────────────────────────────────────────────
def _post_discord_now(payload, return_msg_id=False):
    if not DISCORD_WEBHOOK_URL:
        print("[discord error] Webhook URL missing!")
        return None
    try:
        url = DISCORD_WEBHOOK_URL + ("?wait=true" if return_msg_id else "")
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        if return_msg_id:
            return r.json().get("id")
    except Exception as e:
        print(f"[discord error] {e}")
    return None

def post_discord(payload):
    threading.Thread(target=_post_discord_now, args=(payload,), daemon=True).start()

# ── ENRICHMENT (FOLLOW-UP REPLY) ──────────────────────────────────────────────
def fetch_twitter_user_info(handle):
    try:
        r = requests.get(
            f"https://api.twitter.com/2/users/by/username/{handle}",
            headers=TWITTER_HEADERS,
            params={"user.fields": "public_metrics,verified,created_at"},
            timeout=8
        )
        r.raise_for_status()
        u       = r.json().get("data", {})
        metrics = u.get("public_metrics", {})
        return {
            "verified":    u.get("verified", False),
            "tweet_count": metrics.get("tweet_count", 0),
            "followers":   metrics.get("followers_count", 0),
        }
    except Exception as e:
        print(f"[user info error] {e}")
        return None

def compute_validity_rating(user_info, is_reporter):
    score   = 5
    reasons = []
    if is_reporter:
        score += 3
        reasons.append("✅ Verified MLB beat reporter")
    elif user_info:
        if user_info["verified"]:
            score += 2
            reasons.append("✅ Verified account")
        followers = user_info["followers"]
        if followers >= 50000:
            score += 2
            reasons.append(f"✅ Large following ({followers:,})")
        elif followers >= 10000:
            score += 1
            reasons.append(f"⚠️ Mid-tier following ({followers:,})")
        else:
            score -= 1
            reasons.append(f"⚠️ Small following ({followers:,})")
        tweets = user_info["tweet_count"]
        if tweets >= 10000:
            reasons.append(f"📊 Active account ({tweets:,} tweets)")
        else:
            score -= 1
            reasons.append(f"📊 Low tweet history ({tweets:,} tweets)")
    else:
        reasons.append("❓ Could not fetch account data")
    score = max(1, min(10, score))
    label = "🔴 LOW" if score <= 4 else "🟡 MEDIUM" if score <= 6 else "🟢 HIGH"
    return score, label, reasons

def post_followup_reply(message_id, handle, is_reporter, team, pinch_hitter, replaced, tweet_text):
    """Background: fetch enrichment + run Claude clarifier, then reply to the original Discord alert."""
    def _run():
        if not DISCORD_CHANNEL_ID:
            print("[followup] DISCORD_CHANNEL_ID not set — skipping reply")
            return

        # Fetch Twitter user info and Claude verdict in parallel
        user_info_result  = [None]
        claude_result     = [True, "Pending"]

        def _get_user():
            user_info_result[0] = fetch_twitter_user_info(handle)

        def _claude_done(is_valid, reasoning):
            claude_result[0] = is_valid
            claude_result[1] = reasoning

        t1 = threading.Thread(target=_get_user)
        t1.start()
        claude_clarify(tweet_text, pinch_hitter, replaced, _claude_done)
        t1.join(timeout=12)
        time.sleep(12)  # Give Claude time to respond

        user_info = user_info_result[0]
        score, label, reasons = compute_validity_rating(user_info, is_reporter)

        ai_verdict = "✅ AI confirmed — pre-event alert" if claude_result[0] else "⚠️ AI flagged — may be invalid"

        lines = [f"**📊 Alert Enrichment — @{handle}**\n"]
        lines.append(f"🏟️ **Teams involved:** {team}")
        lines.append(f"🤖 **AI Review:** {ai_verdict}")

        if user_info:
            lines.append(
                f"👤 **Account:** "
                f"{'✅ Verified' if user_info['verified'] else 'Unverified'} · "
                f"{user_info['followers']:,} followers · "
                f"{user_info['tweet_count']:,} tweets"
            )
        else:
            lines.append("👤 **Account:** Could not fetch info")

        if is_reporter:
            lines.append(f"🎙️ **Source type:** Official {team} beat reporter")
        else:
            lines.append("🌐 **Source type:** General Twitter user")

        lines.append(f"\n**Validity: {score}/10 — {label}**")
        for reason in reasons:
            lines.append(f"  {reason}")

        payload = {
            "content": "\n".join(lines),
            "message_reference": {
                "message_id":         message_id,
                "channel_id":         DISCORD_CHANNEL_ID,
                "fail_if_not_exists": False,
            }
        }
        try:
            requests.post(DISCORD_WEBHOOK_URL + "?wait=true", json=payload, timeout=10).raise_for_status()
            print(f"  📎 Follow-up reply posted (msg_id={message_id}, AI={'✅' if claude_result[0] else '⚠️'})")
        except Exception as e:
            print(f"[followup error] {e}")

    threading.Thread(target=_run, daemon=True).start()

# ── ALERT SENDERS ─────────────────────────────────────────────────────────────
def post_reporter_alert(handle, text, url, team, pinch_hitter, replaced):
    summary       = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed_payload = {
        "content": "@everyone 🔥 BEAT REPORTER",
        "embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
            "description": (
                f"**Verified beat reporter — pre-event pinch hit**\n\n"
                f"📋 **{summary}**\n\n"
                f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
                f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
            ),
            "color": 0x00FF00,
            "footer": {"text": f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]
    }

    def _send():
        msg_id = _post_discord_now(embed_payload, return_msg_id=True)
        if msg_id:
            post_followup_reply(msg_id, handle, True, team, pinch_hitter, replaced, text)

    threading.Thread(target=_send, daemon=True).start()
    print(f"  🟢 Reporter: {team} — {summary}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced):
    summary       = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    embed_payload = {
        "content": "@everyone",
        "embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
            "description": (
                f"**Twitter source — pre-event pinch hit**\n\n"
                f"📋 **{summary}**\n\n"
                f"🌐 **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
                f"💰 **BET THE UNDER NOW**"
            ),
            "color": 0xF1C40F,
            "footer": {"text": f"General Alert · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]
    }

    def _send():
        msg_id = _post_discord_now(embed_payload, return_msg_id=True)
        if msg_id:
            post_followup_reply(msg_id, handle, False, team, pinch_hitter, replaced, text)

    threading.Thread(target=_send, daemon=True).start()
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
        print(f"  🚫 @{handle}: '{pinch_hitter}' or '{replaced}' not in today's rosters")
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
    url    = "https://api.twitter.com/2/tweets/search/stream"
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
    print("⚾ MLB Pinch Hit Bot v10.0")
    print("   ✅ Full 26-man roster prefetch at startup")
    print("   ✅ Last name match: 3+ chars (was 5+)")
    print("   ✅ ASCII normalization — Latin player names supported")
    print("   ✅ Expanded phrase list (8 new phrases)")
    print("   ✅ Claude AI clarifier — fires in background, never blocks alerts")
    print(f"   {len(REPORTERS)} reporters | game hours 12pm-1am ET\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return
    if not DISCORD_CHANNEL_ID:
        print("[warning] DISCORD_CHANNEL_ID not set — follow-up replies will be skipped")
    if not ANTHROPIC_API_KEY:
        print("[warning] ANTHROPIC_API_KEY not set — AI clarifier will be skipped")

    start_lineup_refresh_thread()
    prefetch_full_rosters()
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
