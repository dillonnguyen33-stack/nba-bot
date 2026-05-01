"""
MLB Pinch Hit Alert Bot - Clean Rebuild v3
Fixes from v2:
  1) MLB roster cross-check
  2) All reporters checked every cycle
  3) @mentions stripped before player extraction
  4) Tweet age check — reject tweets older than 10 minutes
New in v3:
  5) Opinion/complaint/hypothetical language rejected
  6) Player cooldown — max 2 alerts per player per 2 hours
"""

import os
import re
import time
import requests
from datetime import datetime, timezone
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY")
POLL_INTERVAL        = 10
ET_TZ                = pytz.timezone("America/New_York")
MAX_TWEET_AGE_SECS   = 600
PLAYER_COOLDOWN_SEC  = 7200   # 2 hours per player
PLAYER_MAX_ALERTS    = 2      # max alerts before cooldown kicks in

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

PRESENT_FUTURE_MARKERS = [
    r'\bis\b', r'\bwill\b', r'\bslated\b', r'\bon\s+deck\b',
    r'\bexpected\b', r'\bgoing\s+to\b', r'\bset\s+to\b',
    r'\bcoming\s+up\b', r'\bheading\b', r'\bwarming\b',
    r'\bscheduled\b', r'\bdue\s+to\b', r'\bappears\b',
    r'\blooks\s+like\b', r'\bunclear\b',
    r'\btaking\s+over\b', r'\bcoming\s+in\b',
]

# ── REJECT PHRASES ────────────────────────────────────────────────────────────
REJECT_PHRASES = [
    # Past tense / results
    "home run", "homered", "hit a", "singled", "doubled", "tripled",
    "drove in", "struck out", "flies out", "grounds out",
    "pinch-hit home run", "pinch hit home run",
    "pinch-hit single", "pinch hit single",
    "pinch-hit rbi", "pinch hit rbi",
    "just hit", "last night", "yesterday",
    "in the 1st", "in the 2nd", "in the 3rd",
    "in the 4th", "in the 5th", "in the 6th",
    "in the 7th", "in the 8th", "in the 9th",
    "went 1-for", "went 0-for", "went 2-for",
    # Opinion / complaint / hypothetical — NEW in v3
    "why pinch hit", "why would", "should have", "shouldn't have",
    "should not have", "bad decision", "bad manager", "terrible decision",
    "doesn't make sense", "makes no sense", "i hate when", "i don't understand",
    "can't believe", "cannot believe", "questionable",
    "what a waste", "poor decision", "wrong decision",
    "never should", "he keeps", "keeps making", "mistake",
    "would you pinch", "if i were", "hypothetically",
    "in theory", "imagine if", "what if",
    "how often", "it's funny", "its funny", "funny how",
    "rewards players", "punish", "not to blame",
    "i would have", "i would not", "i wouldn't",
    "unless he", "unless they", "unless the",
]

PROP_BOOKS = {
    "draftkings":  "DraftKings",
    "fanduel":     "FanDuel",
    "fliff":       "Fliff",
    "hardrockbet": "Hard Rock",
    "bet365":      "Bet365",
}

MLB_PROP_MARKETS = ["batter_hits", "batter_total_bases", "batter_rbis", "batter_home_runs"]

MLB_TEAM_IDS = {
    "Orioles": 110, "Red Sox": 111, "Yankees": 147, "Rays": 139, "Blue Jays": 141,
    "White Sox": 145, "Guardians": 114, "Tigers": 116, "Royals": 118, "Twins": 142,
    "Astros": 117, "Angels": 108, "Athletics": 133, "Mariners": 136, "Rangers": 140,
    "Braves": 144, "Marlins": 146, "Mets": 121, "Phillies": 143, "Nationals": 120,
    "Cubs": 112, "Reds": 113, "Brewers": 158, "Pirates": 134, "Cardinals": 138,
    "Diamondbacks": 109, "Rockies": 115, "Dodgers": 119, "Padres": 135, "Giants": 137,
}

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids      = set()
posted_alert_keys   = set()
last_reset_date     = None
player_team_map     = {}
last_roster_refresh = 0
player_alert_count  = {}   # {last_name: count}
player_alert_time   = {}   # {last_name: timestamp}

TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

# ── PLAYER COOLDOWN ───────────────────────────────────────────────────────────
def is_player_on_cooldown(player_name):
    if not player_name:
        return False
    key = player_name.lower().split()[-1]
    now = time.time()
    if key in player_alert_time:
        if now - player_alert_time[key] > PLAYER_COOLDOWN_SEC:
            player_alert_count[key] = 0
    return player_alert_count.get(key, 0) >= PLAYER_MAX_ALERTS

def record_player_alert(player_name):
    if not player_name:
        return
    key = player_name.lower().split()[-1]
    player_alert_count[key] = player_alert_count.get(key, 0) + 1
    player_alert_time[key]  = time.time()
    print(f"  📊 Player alert count: {key} = {player_alert_count[key]}")

# ── ROSTER LOOKUP ─────────────────────────────────────────────────────────────
def build_player_team_map():
    global player_team_map, last_roster_refresh
    now = time.time()
    if now - last_roster_refresh < 21600 and player_team_map:
        return
    print("[roster] Refreshing MLB roster lookup...")
    new_map = {}
    for team_name, team_id in MLB_TEAM_IDS.items():
        try:
            r = requests.get(
                f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster",
                params={"rosterType": "active"}, timeout=10
            )
            r.raise_for_status()
            for player in r.json().get("roster", []):
                full_name = player.get("person", {}).get("fullName", "")
                if not full_name:
                    continue
                parts    = full_name.split()
                last     = parts[-1].lower()
                full_low = full_name.lower()
                new_map[last]     = team_name
                new_map[full_low] = team_name
                if len(parts) >= 2:
                    new_map[parts[0].lower() + " " + last] = team_name
            time.sleep(0.2)
        except Exception as e:
            print(f"[roster error] {team_name}: {e}")
    player_team_map     = new_map
    last_roster_refresh = now
    print(f"[roster] {len(new_map)} entries loaded\n")

def lookup_player_team(name):
    if not name or not player_team_map:
        return None
    nl = name.lower().strip()
    if nl in player_team_map:
        return player_team_map[nl]
    return player_team_map.get(nl.split()[-1])

def is_mlb_player(name):
    return lookup_player_team(name) is not None

def infer_team_from_text(text):
    tl = text.lower()
    for alias, team in TEAM_ALIASES.items():
        if alias in tl:
            return team
    words = text.split()
    for i, word in enumerate(words):
        if i < len(words) - 1:
            two = (word + " " + words[i+1]).lower()
            if two in player_team_map:
                return player_team_map[two]
        if word.lower() in player_team_map:
            return player_team_map[word.lower()]
    return None

# ── GAME HOURS / RESET ────────────────────────────────────────────────────────
def is_game_hours():
    hour = datetime.now(ET_TZ).hour
    return hour >= 12 or hour == 0

def maybe_reset_daily():
    global seen_tweet_ids, last_reset_date, posted_alert_keys
    global player_alert_count, player_alert_time
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print("[reset] New day — clearing state")
        seen_tweet_ids      = set()
        posted_alert_keys   = set()
        player_alert_count  = {}
        player_alert_time   = {}
        last_reset_date     = today

# ── TWEET AGE CHECK ───────────────────────────────────────────────────────────
def is_recent(created_at_str):
    if not created_at_str:
        return True
    try:
        tweet_time = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - tweet_time).total_seconds()
        return age <= MAX_TWEET_AGE_SECS
    except:
        return True

# ── DETECTION ─────────────────────────────────────────────────────────────────
def strip_mentions(text):
    return re.sub(r'@\w+', '', text)

def is_present_future(text):
    tl = text.lower()
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return False, f"rejected: '{phrase}'"
    for marker in PRESENT_FUTURE_MARKERS:
        if re.search(marker, text, re.IGNORECASE):
            return True, f"matched: '{marker}'"
    return False, "no present/future marker"

def extract_players(text):
    clean = strip_mentions(text)
    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:on\s+deck\s+to\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?(?:bat|hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+slated\s+to\s+pinch[- ]hit\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+taking\s+over\s+\w+\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), m.group(2).strip()

    patterns_hitter = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:on\s+deck|slated|expected|set)\s+to\s+pinch[- ]hit',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?pinch[- ]hitting',
        r'(?:ph|pinch[- ]hit(?:ting)?)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_hitter:
        m = re.search(p, clean)
        if m:
            return m.group(1).strip(), None

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
    except:
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
            except:
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
    if pinch_hitter and replaced:
        summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    elif pinch_hitter:
        summary = f"**{pinch_hitter}** is being called to pinch hit"
    elif replaced:
        summary = f"**{replaced}** is coming out — pinch hitter incoming"
    else:
        summary = "Pinch hit situation — see tweet below"

    embed = {"embeds": [{"title": f"🔥⚾ BEAT REPORTER ALERT — {team}",
        "description": (
            f"**Verified beat reporter tweeting pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🎙️ **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"{format_lines(lines_data)}\n\n"
            f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
        ),
        "color": 0x00FF00,
        "footer": {"text": f"Beat Reporter · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]}
    post_discord({"content": "@everyone 🔥 BEAT REPORTER", "embeds": embed["embeds"]})
    print(f"  🟢 Reporter alert: {team} — {summary}")

def post_general_alert(handle, text, url, team, pinch_hitter, replaced, lines_data):
    if pinch_hitter and replaced:
        summary = f"**{pinch_hitter}** will pinch hit for **{replaced}**"
    elif pinch_hitter:
        summary = f"**{pinch_hitter}** is being called to pinch hit"
    elif replaced:
        summary = f"**{replaced}** is coming out — pinch hitter incoming"
    else:
        summary = "Pinch hit situation — see tweet below"

    embed = {"embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
        "description": (
            f"**Twitter source reporting pre-event pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            f"🌐 **@{handle}:**\n_{text[:200]}_\n🔗 [View Tweet]({url})\n\n"
            f"{format_lines(lines_data)}\n\n"
            f"💰 **BET THE UNDER ON ALL LINES NOW**"
        ),
        "color": 0xF1C40F,
        "footer": {"text": f"General Alert · {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}}]}
    post_discord({"content": "@everyone", "embeds": embed["embeds"]})
    print(f"  🟡 General alert: {team} — {summary}")

# ── TWITTER ───────────────────────────────────────────────────────────────────
def search_tweets(query, max_results=15):
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=TWITTER_HEADERS,
            params={
                "query":        query,
                "max_results":  max_results,
                "tweet.fields": "created_at,author_id,text",
                "expansions":   "author_id",
                "user.fields":  "username",
            },
            timeout=15
        )
        if r.status_code == 400:
            print(f"[twitter 400] {query[:60]}")
            return {}, []
        if r.status_code == 503:
            print(f"[twitter 503] service unavailable")
            return {}, []
        r.raise_for_status()
        data   = r.json()
        tweets = data.get("data", [])
        users  = {u["id"]: u["username"].lower()
                  for u in data.get("includes", {}).get("users", [])}
        return users, tweets
    except Exception as e:
        print(f"[twitter error] {e}")
        return {}, []

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
    except:
        return []

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

# ── PROCESS AND ALERT ─────────────────────────────────────────────────────────
def process_and_alert(tweets, users):
    for tweet in tweets:
        tid        = tweet.get("id") or ""
        text       = tweet.get("text", "")
        aid        = tweet.get("author_id", "")
        handle     = users.get(aid, "unknown")
        created_at = tweet.get("created_at", "")

        # Reject old tweets
        if not is_recent(created_at):
            continue

        # Deduplicate
        if tid in seen_tweet_ids:
            continue
        seen_tweet_ids.add(tid)

        # Core phrase check
        tl = text.lower()
        core_phrases = [
            "on deck to pinch hit",
            "slated to pinch hit",
            "pinch hit for",
            "pinch-hit for",
            "will pinch hit",
        ]
        if not any(phrase in tl for phrase in core_phrases):
            continue

        # Present/future + opinion filter
        is_live, reason = is_present_future(text)
        if not is_live:
            print(f"  🚫 @{handle}: {reason} — {text[:60]}")
            continue

        # Extract players (with @mention stripping)
        pinch_hitter, replaced = extract_players(text)

        # MLB roster cross-check
        ph_is_mlb  = is_mlb_player(pinch_hitter) if pinch_hitter else False
        rep_is_mlb = is_mlb_player(replaced) if replaced else False

        if pinch_hitter and replaced and not ph_is_mlb and not rep_is_mlb:
            print(f"  🚫 Neither '{pinch_hitter}' nor '{replaced}' on MLB roster")
            continue
        elif pinch_hitter and not replaced and not ph_is_mlb:
            print(f"  🚫 '{pinch_hitter}' not on MLB roster")
            continue
        elif replaced and not pinch_hitter and not rep_is_mlb:
            print(f"  🚫 '{replaced}' not on MLB roster")
            continue

        # ── FIX 5: player cooldown check ─────────────────────────────────────
        key_player = pinch_hitter or replaced
        if is_player_on_cooldown(key_player):
            print(f"  🔇 @{handle}: '{key_player}' on cooldown — too many alerts already")
            continue

        # Determine team
        is_reporter = handle in REPORTER_HANDLES
        reporter    = REPORTER_BY_HANDLE.get(handle)
        team        = reporter["team"] if reporter else None

        if not team:
            if pinch_hitter:
                team = lookup_player_team(pinch_hitter)
            if not team and replaced:
                team = lookup_player_team(replaced)
            if not team:
                team = infer_team_from_text(text)
            if not team:
                team = "Unknown Team"

        url       = f"https://twitter.com/{handle}/status/{tid}"
        alert_key = tid

        if alert_key in posted_alert_keys:
            continue
        posted_alert_keys.add(alert_key)

        print(f"  ✅ VALID: @{handle} ({'reporter' if is_reporter else 'general'}) "
              f"team={team} ph={pinch_hitter} out={replaced}")
        print(f"     Tweet: {text[:100]}")

        # Record alert for cooldown tracking
        record_player_alert(key_player)

        lines_data = get_player_lines(pinch_hitter) if pinch_hitter else {}

        if is_reporter:
            post_reporter_alert(handle, text, url, team, pinch_hitter, replaced, lines_data)
        else:
            post_general_alert(handle, text, url, team, pinch_hitter, replaced, lines_data)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot v3")
    print("   Fix 5: Opinion/complaint/hypothetical language rejected")
    print("   Fix 6: Player cooldown — max 2 alerts per player per 2 hours")
    print(f"   {len(REPORTERS)} reporters | poll={POLL_INTERVAL}s\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return

    build_player_team_map()

    print("Looking up reporter user IDs...")
    handles  = [r["handle"] for r in REPORTERS]
    user_ids = {}
    for i in range(0, len(handles), 100):
        user_ids.update(get_user_ids_batch(handles[i:i+100]))
    print(f"Found {len(user_ids)} user IDs\n")

    cycle = 0

    while True:
        maybe_reset_daily()
        build_player_team_map()

        hour = datetime.now(ET_TZ).hour
        if not (hour >= 12 or hour == 0):
            print(f"[{datetime.now(ET_TZ).strftime('%H:%M ET')}] Outside game hours — sleeping 10 min")
            time.sleep(600)
            continue

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle {cycle}")

        users1, tweets1 = search_tweets('"on deck to pinch hit" -is:retweet lang:en', 15)
        print(f"   'on deck to pinch hit': {len(tweets1)} tweets")
        process_and_alert(tweets1, users1)
        time.sleep(2)

        users2, tweets2 = search_tweets('"slated to pinch hit" -is:retweet lang:en', 15)
        print(f"   'slated to pinch hit': {len(tweets2)} tweets")
        process_and_alert(tweets2, users2)
        time.sleep(2)

        users3, tweets3 = search_tweets('"pinch hit for" -is:retweet lang:en', 15)
        print(f"   'pinch hit for': {len(tweets3)} tweets")
        process_and_alert(tweets3, users3)
        time.sleep(2)

        users4, tweets4 = search_tweets('"pinch-hit for" -is:retweet lang:en', 15)
        print(f"   'pinch-hit for': {len(tweets4)} tweets")
        process_and_alert(tweets4, users4)
        time.sleep(2)

        users5, tweets5 = search_tweets('"will pinch hit" -is:retweet lang:en', 15)
        print(f"   'will pinch hit': {len(tweets5)} tweets")
        process_and_alert(tweets5, users5)
        time.sleep(2)

        rep_checked = 0
        for reporter in REPORTERS:
            handle = reporter["handle"].lower()
            uid    = user_ids.get(handle)
            if not uid:
                continue
            tweets = get_user_tweets(uid, max_results=3)
            for t in tweets:
                t["author_id"] = uid
            process_and_alert(tweets, {uid: handle})
            rep_checked += 1
            time.sleep(0.5)

        print(f"   Checked {rep_checked} reporter timelines")
        cycle += 1
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
