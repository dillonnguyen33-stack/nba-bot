"""
MLB Pinch Hit Alert Bot — v17.0

Changes from v16.0:
- LATENCY: Reporters now fire IMMEDIATELY on cheap-filter pass (trusted source);
           Claude runs ASYNC only to fill in structured names / retract if it
           turns out invalid. Removes the ~1-2s Anthropic round-trip from the
           critical path for the alerts that matter most.
- LATENCY: Claude timeout 15s -> 5s (a slow Anthropic call could silently delay
           an alert past any bettable window).
- MEASURE: Every alert now logs pipeline latency (now - tweet.created_at) in the
           footer, e.g. "fired 2.3s after tweet". Tells you how much of a miss is
           your pipeline vs. the reporter being slow.
- Non-reporter path unchanged (still gates on Claude + roster).
- ANTHROPIC_API_KEY now optional: reporters fire without it; only non-reporters
  are disabled if it's missing.
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
DISCORD_LOG_WEBHOOK  = os.environ.get("DISCORD_LOG_WEBHOOK_URL")
ET_TZ                = ZoneInfo("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2
LINEUP_REFRESH_SECS  = 300

# ── ASCII NORMALIZATION ───────────────────────────────────────────────────────
def normalize(text):
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode("ascii").lower()

# ── SUFFIX STRIPPING ──────────────────────────────────────────────────────────
_SUFFIXES = re.compile(r'\b(jr\.?|sr\.?|ii|iii|iv)\s*$', re.IGNORECASE)

def strip_suffix(name):
    return _SUFFIXES.sub('', normalize(name)).strip()

# ── TWEET LATENCY ─────────────────────────────────────────────────────────────
def _tweet_latency(created_at):
    """Seconds from tweet creation to now (your pipeline latency). None if unknown.
    Twitter created_at is ISO 8601, e.g. '2024-06-01T23:15:30.000Z'."""
    if not created_at:
        return None
    try:
        t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None

def _latency_str(latency):
    return f"{latency:.1f}s" if latency is not None else "n/a"

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

# ── STREAM RULES ──────────────────────────────────────────────────────────────
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

# ── CHEAP STRING PRE-FILTERS (run before Claude to save API calls) ────────────
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

def has_core_phrase(text):
    tl = normalize(text)
    return any(phrase in tl for phrase in PINCH_HIT_PHRASES_LOWER)

def has_reject_phrase(text):
    tl = normalize(text)
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return True, phrase
    return False, None

def is_question(text):
    stripped = text.strip()
    if re.search(r"\?[\s\W]*$", stripped):
        return True
    if re.match(r"(did|does|would|should|could|can)\b", stripped.lower()):
        return True
    return False

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

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids     = set()
posted_alert_keys  = set()
last_reset_date    = None
player_alert_count = {}
player_alert_time  = {}
daily_lineup_map   = {}
last_lineup_refresh = 0
_name_index        = {}
_name_lock         = threading.Lock()
_lineup_lock       = threading.Lock()

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
        _name_index.clear()
        last_reset_date     = today

# ── PLAYER COOLDOWN ───────────────────────────────────────────────────────────
def is_player_on_cooldown(name):
    if not name:
        return False
    key = strip_suffix(name).split()[-1]
    now = time.time()
    if key in player_alert_time:
        if now - player_alert_time[key] > PLAYER_COOLDOWN_SEC:
            player_alert_count[key] = 0
    return player_alert_count.get(key, 0) >= PLAYER_MAX_ALERTS

def record_player_alert(name):
    if not name:
        return
    key = strip_suffix(name).split()[-1]
    player_alert_count[key] = player_alert_count.get(key, 0) + 1
    player_alert_time[key]  = time.time()

# ── ROSTER / NAME INDEX ───────────────────────────────────────────────────────
def _add_player_to_map(target_map, full_name, team_name):
    if not full_name:
        return
    norm  = strip_suffix(full_name)
    parts = norm.split()
    target_map[norm] = team_name
    if len(parts) >= 2:
        target_map[parts[0] + " " + parts[-1]] = team_name
    if len(parts) >= 1 and len(parts[-1]) >= 3:
        target_map["_last_" + parts[-1]] = team_name
    if len(parts) >= 1 and len(parts[0]) >= 5:
        target_map["_first_" + parts[0]] = team_name

def rebuild_index(new_entries):
    with _name_lock:
        _name_index.update(new_entries)

def _levenshtein(a, b):
    if abs(len(a) - len(b)) > 1:
        return 2
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(prev[j] + (ca != cb), prev[j+1] + 1, curr[j] + 1))
        prev = curr
    return prev[-1]

def _fuzzy_lookup(key, index):
    if key in index:
        return index[key]
    if len(key) < 5:
        return None
    for ikey, team in index.items():
        if ikey.startswith("_"):
            continue
        if abs(len(ikey) - len(key)) > 1:
            continue
        if _levenshtein(key, ikey) == 1:
            return team
    return None

def _resolve_name(name):
    if not name:
        return None
    with _name_lock:
        idx = dict(_name_index)
    norm  = strip_suffix(name)
    parts = norm.split()
    if norm in idx:
        return idx[norm]
    if len(parts) >= 2:
        fl = parts[0] + " " + parts[-1]
        if fl in idx:
            return idx[fl]
    if parts and len(parts[-1]) >= 3:
        key = "_last_" + parts[-1]
        if key in idx:
            return idx[key]
    if parts and len(parts[0]) >= 5:
        key = "_first_" + parts[0]
        if key in idx:
            return idx[key]
    if len(norm) >= 5:
        result = _fuzzy_lookup(norm, idx)
        if result:
            return result
    if parts and len(parts[-1]) >= 5:
        last_idx = {k[6:]: v for k, v in idx.items() if k.startswith("_last_")}
        result = _fuzzy_lookup(parts[-1], last_idx)
        if result:
            return result
    return None

def lookup_player_team(name):
    return _resolve_name(name)

def infer_team_from_text(text):
    tl = normalize(text)
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    return None

# ── ROSTER PREFETCH ───────────────────────────────────────────────────────────
def prefetch_full_rosters():
    def _run():
        try:
            teams_r = requests.get(
                "https://statsapi.mlb.com/api/v1/teams",
                params={"sportId": 1}, timeout=10
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
                    params={"rosterType": "active"}, timeout=10
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
        rebuild_index(new_entries)
        print(f"[roster] Full rosters loaded — {len(new_entries)} entries\n")

    threading.Thread(target=_run, daemon=True).start()

def start_lineup_refresh_thread():
    def _do_refresh():
        global last_lineup_refresh
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
        rebuild_index(new_map)
        print(f"[lineup] {len([k for k in new_map if not k.startswith('_')])} players loaded\n")

    def loop():
        while True:
            try:
                _do_refresh()
            except Exception as e:
                print(f"[lineup] Refresh error: {e}")
            time.sleep(LINEUP_REFRESH_SECS)

    threading.Thread(target=loop, daemon=True).start()
    print("[lineup] Background refresh thread started (every 5 min)\n")

# ── CLAUDE — NAME EXTRACTOR + VALIDATOR ───────────────────────────────────────
CLAUDE_SYSTEM = """You are an MLB pinch hit alert parser. Extract player names and classify tweets.

Given a tweet, respond with ONLY a JSON object (no markdown, no explanation):
{
  "is_valid": true or false,
  "pinch_hitter": "Full Name" or null,
  "replaced": "Full Name" or null,
  "reason": "one short sentence"
}

is_valid = true ONLY if ALL of these are true:
1. A specific named player is ABOUT TO pinch hit (pre-event, imminent or confirmed)
2. A specific named player is being replaced (named or clearly identified)
3. This is an MLB game (not college, minor league, high school, softball, or non-baseball usage)
4. The at-bat has NOT happened yet (no past-tense results like singled, homered, struck out)
5. It is not a question, speculation, fan demand, or hypothetical

Extract real player names even from complex sentence structures. Examples:
- "Brendan Donovan has come on deck to pinch hit for Cal Raleigh" → pinch_hitter: "Brendan Donovan", replaced: "Cal Raleigh", is_valid: true
- "Casey Schmitt needs to pinch hit for Chapman" → is_valid: false (fan demand, not confirmed)
- "He'll pinch hit for Kelenic when a lefty is in" → is_valid: false (conditional/hypothetical)
- "Soto was pinch hit for in the 7th" → is_valid: false (already happened)
- "Should they pinch hit for him?" → is_valid: false (question)
- "warming up to pinch hit for Leon" → is_valid: true (imminent pre-event)
- "Tonight Brian returns to pinch-hit for Laura on the IngrahamAngle" → is_valid: false (not baseball)
- "pinch hit for Lutterman… #Hokies" → is_valid: false (college baseball)

For is_valid=true, both pinch_hitter and replaced must be non-null."""

def claude_classify(tweet_text, callback):
    if not ANTHROPIC_API_KEY:
        callback(False, None, None, "No API key")
        return

    def _run():
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-haiku-4-5-20251001",
                    "max_tokens": 150,
                    "system":     CLAUDE_SYSTEM,
                    "messages":   [{"role": "user", "content": f'Tweet: "{tweet_text}"'}],
                },
                timeout=5  # v17: was 15s — a slow call must not delay an alert
            )
            r.raise_for_status()
            raw = r.json()["content"][0]["text"].strip()
            raw = re.sub(r"^```json\s*", "", raw)
            raw = re.sub(r"```\s*$", "", raw)
            data = json.loads(raw)
            callback(
                bool(data.get("is_valid")),
                data.get("pinch_hitter"),
                data.get("replaced"),
                data.get("reason", "")
            )
        except Exception as e:
            print(f"[claude] Error: {e}")
            callback(False, None, None, f"Claude error: {e}")

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

def _post_reply(message_id, content):
    """Post a threaded reply to an existing alert message (name enrichment / retract)."""
    if not (message_id and DISCORD_CHANNEL_ID and DISCORD_WEBHOOK_URL):
        return
    payload = {
        "content": content,
        "message_reference": {
            "message_id":         message_id,
            "channel_id":         DISCORD_CHANNEL_ID,
            "fail_if_not_exists": False,
        },
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL + "?wait=true", json=payload, timeout=10)
    except Exception as e:
        print(f"[reply error] {e}")

# ── DROP LOG ──────────────────────────────────────────────────────────────────
def post_drop_log(handle, reason, text):
    if not DISCORD_LOG_WEBHOOK:
        return
    def _send():
        try:
            timestamp = datetime.now(ET_TZ).strftime("%H:%M:%S ET")
            payload = {
                "content": (
                    f"`{timestamp}` 🚫 **@{handle}** dropped — *{reason}*\n"
                    f">>> {text[:200]}"
                )
            }
            requests.post(DISCORD_LOG_WEBHOOK, json=payload, timeout=8)
        except Exception as e:
            print(f"[log error] {e}")
    threading.Thread(target=_send, daemon=True).start()

# ── ENRICHMENT (general alerts only) ──────────────────────────────────────────
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

def post_followup_reply(message_id, handle, is_reporter, team, pinch_hitter, replaced):
    def _run():
        if not DISCORD_CHANNEL_ID:
            return
        user_info = fetch_twitter_user_info(handle)
        score, label, reasons = compute_validity_rating(user_info, is_reporter)
        lines = [f"**📊 Alert Enrichment — @{handle}**\n"]
        lines.append(f"🏟️ **Teams involved:** {team}")
        if user_info:
            lines.append(
                f"👤 **Account:** "
                f"{'✅ Verified' if user_info['verified'] else 'Unverified'} · "
                f"{user_info['followers']:,} followers · "
                f"{user_info['tweet_count']:,} tweets"
            )
        else:
            lines.append("👤 **Account:** Could not fetch info")
        lines.append("🌐 **Source type:** General Twitter user")
        lines.append(f"\n**Validity: {score}/10 — {label}**")
        for reason in reasons:
            lines.append(f"  {reason}")
        _post_reply(message_id, "\n".join(lines))
    threading.Thread(target=_run, daemon=True).start()

# ── ALERT SENDERS ─────────────────────────────────────────────────────────────
def post_reporter_alert_fast(handle, text, url, team, latency):
    """v17: Fire the reporter alert IMMEDIATELY (trusted source, cheap filters
    already passed). Claude runs ASYNC only to fill in the structured names or
    retract if it turns out invalid — it is NOT on the critical path."""
    footer = f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    if latency is not None:
        footer += f" · fired {latency:.1f}s after tweet"
    embed_payload = {
        "content": "@everyone 🔥 BEAT REPORTER",
        "embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
            "description": (
                f"**Verified beat reporter — pre-event pinch hit**\n\n"
                f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
                f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
            ),
            "color": 0x00FF00,
            "footer": {"text": footer}}]
    }

    def _send():
        msg_id = _post_discord_now(embed_payload, return_msg_id=True)

        def on_claude(is_valid, ph, out, reason):
            if is_valid and ph and out:
                record_player_alert(ph)
                _post_reply(msg_id, f"📋 **{ph}** will pinch hit for **{out}**")
            elif reason and not reason.startswith("No API key") \
                    and not reason.startswith("Claude error"):
                _post_reply(msg_id, f"⚠️ Auto-check flagged this as possibly invalid: _{reason}_")

        claude_classify(text, on_claude)

    threading.Thread(target=_send, daemon=True).start()
    print(f"  ⚡🟢 Reporter (fast fire): {team} — @{handle} | latency {_latency_str(latency)}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced, latency=None):
    summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    footer = f"General Alert · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    if latency is not None:
        footer += f" · fired {latency:.1f}s after tweet"
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
            "footer": {"text": footer}}]
    }
    def _send():
        msg_id = _post_discord_now(embed_payload, return_msg_id=True)
        if msg_id:
            post_followup_reply(msg_id, handle, False, team, pinch_hitter, replaced)
    threading.Thread(target=_send, daemon=True).start()
    print(f"  🟡 General: {team} — {summary} | latency {_latency_str(latency)}")

# ── CORE: FIRE ALERT (non-reporters only) ─────────────────────────────────────
def _fire_alert(handle, text, tid, pinch_hitter, replaced, latency=None):
    ph_team  = lookup_player_team(pinch_hitter)
    out_team = lookup_player_team(replaced)
    if not ph_team or not out_team:
        missing = []
        if not ph_team:  missing.append(pinch_hitter)
        if not out_team: missing.append(replaced)
        print(f"  🚫 @{handle}: roster miss for {missing}")
        post_drop_log(handle, f"roster miss: {', '.join(missing)}", text)
        return

    if is_player_on_cooldown(pinch_hitter):
        print(f"  🔇 '{pinch_hitter}' on cooldown")
        post_drop_log(handle, f"cooldown: {pinch_hitter}", text)
        return

    team = lookup_player_team(pinch_hitter) or lookup_player_team(replaced) \
           or infer_team_from_text(text) or "Unknown Team"

    url = f"https://twitter.com/{handle}/status/{tid}"
    if tid in posted_alert_keys:
        return
    posted_alert_keys.add(tid)

    print(f"  ✅ VALID: @{handle} (general) team={team} | {pinch_hitter} for {replaced}")
    print(f"     {text[:120]}")

    record_player_alert(pinch_hitter)
    post_general_alert(handle, text, url, team, pinch_hitter, replaced, latency)

# ── CORE: HANDLE SINGLE TWEET ─────────────────────────────────────────────────
def handle_tweet(tid, text, handle, created_at=None):
    maybe_reset_daily()

    if not is_game_hours():
        return
    if not tid or tid in seen_tweet_ids:
        return
    seen_tweet_ids.add(tid)

    # ── Stage 1: Cheap string pre-filters ────────────────────────────────────
    if not has_core_phrase(text):
        return

    if is_question(text):
        print(f"  🚫 @{handle}: question/speculation — {text[:60]}")
        post_drop_log(handle, "question/speculation", text)
        return

    if result_comes_after_ph(text):
        print(f"  🚫 @{handle}: result after ph (already happened) — {text[:60]}")
        post_drop_log(handle, "already happened", text)
        return

    rejected, phrase = has_reject_phrase(text)
    if rejected:
        print(f"  🚫 @{handle}: '{phrase}' — {text[:60]}")
        post_drop_log(handle, f"reject phrase: {phrase}", text)
        return

    latency = _tweet_latency(created_at)

    # ── REPORTER FAST PATH (v17): fire NOW, Claude enriches async ─────────────
    if handle in REPORTER_HANDLES:
        if tid in posted_alert_keys:
            return
        posted_alert_keys.add(tid)
        reporter = REPORTER_BY_HANDLE.get(handle)
        team = reporter["team"] if reporter else (infer_team_from_text(text) or "Unknown Team")
        url  = f"https://twitter.com/{handle}/status/{tid}"
        print(f"  ⚡ FAST REPORTER FIRE: @{handle} team={team} | {text[:100]}")
        post_reporter_alert_fast(handle, text, url, team, latency)
        return

    # ── NON-REPORTER: gate on Claude as before ───────────────────────────────
    if not ANTHROPIC_API_KEY:
        print(f"  🚫 @{handle}: no Claude API key (non-reporter) — {text[:60]}")
        post_drop_log(handle, "no Claude API key", text)
        return

    print(f"  🤖 Sending to Claude: @{handle} — {text[:80]}")

    _tid, _text, _handle, _latency = tid, text, handle, latency

    def on_claude_result(is_valid, ph, out, reason):
        if not is_valid:
            print(f"  🚫 @{_handle}: Claude rejected — {reason}")
            post_drop_log(_handle, f"Claude: {reason}", _text)
            return
        if not ph or not out:
            print(f"  🚫 @{_handle}: Claude valid but couldn't extract names")
            post_drop_log(_handle, "Claude: valid but names unclear", _text)
            return
        print(f"  🤖 Claude extracted: ph='{ph}' out='{out}' — {reason}")
        _fire_alert(_handle, _text, _tid, ph, out, _latency)

    claude_classify(text, on_claude_result)

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
                    users.get(tweet_data.get("author_id", ""), "unknown"),
                    tweet_data.get("created_at"),
                )
        except requests.exceptions.ChunkedEncodingError:
            print(f"[stream] Dropped — reconnecting in {reconnect_wait}s...")
        except requests.exceptions.ConnectionError:
            print(f"[stream] Connection error — reconnecting in {reconnect_wait}s...")
        except Exception as e:
            print(f"[stream] Error: {e} — reconnecting in {reconnect_wait}s...")
        time.sleep(reconnect_wait)
        reconnect_wait = min(reconnect_wait * 2, 300)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot v17.0")
    print("   ⚡ Reporters fire IMMEDIATELY (Claude async only for names/retract)")
    print("   ⏱️ Claude timeout 15s -> 5s")
    print("   📏 Every alert logs pipeline latency (now - tweet.created_at)")
    print(f"   {len(REPORTERS)} reporters tracked | game hours 12pm-1am ET\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return
    if not DISCORD_CHANNEL_ID:
        print("[warning] DISCORD_CHANNEL_ID not set — follow-up replies skipped")
    if not ANTHROPIC_API_KEY:
        print("[warning] ANTHROPIC_API_KEY not set — reporters still fire; "
              "non-reporter alerts disabled")
    if not DISCORD_LOG_WEBHOOK:
        print("[warning] DISCORD_LOG_WEBHOOK_URL not set — drop logging skipped")

    start_lineup_refresh_thread()
    prefetch_full_rosters()
    time.sleep(5)

    setup_stream_rules()
    run_stream()

if __name__ == "__main__":
    run()
