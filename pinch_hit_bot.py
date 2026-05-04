"""
MLB Pinch Hit Alert Bot - v5
Key changes:
  - Twitter Filtered Stream (real-time push instead of polling)
  - REQUIRE at least one MLB player name — no player = no alert
  - Tighter opinion/hypothetical/college filters
  - Auto-reconnect on stream disconnect
"""

import os
import re
import time
import json
import requests
from datetime import datetime, timezone
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY")
ET_TZ                = pytz.timezone("America/New_York")
PLAYER_COOLDOWN_SEC  = 7200
PLAYER_MAX_ALERTS    = 2

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
    r'\bout\s+of\s+the\s+game\b', r'\bleft\s+the\s+game\b',
]

# ── REJECT PHRASES ─────────────────────────────────────────────────────────────
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
    # Opinion / complaint
    "why pinch hit", "why would", "should have", "shouldn't have",
    "should not have", "bad decision", "bad manager", "terrible decision",
    "doesn't make sense", "makes no sense", "i hate when",
    "can't believe", "cannot believe", "questionable",
    "what a waste", "poor decision", "wrong decision",
    "never should", "he keeps", "keeps making", "mistake",
    "would you pinch", "if i were", "hypothetically",
    "in theory", "imagine if", "what if",
    "how often", "it's funny", "its funny", "funny how",
    "rewards players", "punish", "not to blame",
    "i would have", "i would not", "i wouldn't",
    "unless he", "unless they", "unless the",
    # Hypotheticals
    "i predict", "i would probably", "i'd probably", "i'd pinch",
    "always gets pinch", "routinely being", "routinely pinch",
    "they tend to", "they usually", "he usually", "he always",
    "probably pinch hit", "likely pinch hit", "might pinch hit",
    "could pinch hit", "would pinch hit", "may pinch hit",
    "i think", "bet they", "bet he", "so i predict",
    "tomorrow", "next game", "next at bat", "next time",
    "i really", "really expected", "i expected",
    "my expectations", "for the record",
    # Questions / complaints
    "how is he not", "why is he not", "how is she not",
    "not in the lineup", "not starting", "shouldn't be",
    "how does", "why does", "why do they",
    "?)",  # parenthetical questions like "(And routinely being pinch hit for?)"
    # College / non-MLB
    "mississippi state", "mississippi st", "husker",
    "college", "university", "high school",
    "ncaa", "minor league", "minors", "triple-a", "triple a", "double-a",
    "farm team", "prospect", "affiliate",
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

MLB_TEAM_IDS = {
    "Orioles": 110, "Red Sox": 111, "Yankees": 147, "Rays": 139, "Blue Jays": 141,
    "White Sox": 145, "Guardians": 114, "Tigers": 116, "Royals": 118, "Twins": 142,
    "Astros": 117, "Angels": 108, "Athletics": 133, "Mariners": 136, "Rangers": 140,
    "Braves": 144, "Marlins": 146, "Mets": 121, "Phillies": 143, "Nationals": 120,
    "Cubs": 112, "Reds": 113, "Brewers": 158, "Pirates": 134, "Cardinals": 138,
    "Diamondbacks": 109, "Rockies": 115, "Dodgers": 119, "Padres": 135, "Giants": 137,
}

# Stream filter rules — exact phrases that must appear in tweets
STREAM_RULES = [
    {"value": '"pinch hit for" -is:retweet lang:en',        "tag": "pinch_hit_for"},
    {"value": '"pinch-hit for" -is:retweet lang:en',        "tag": "pinch_hit_for_hyph"},
    {"value": '"on deck to pinch hit" -is:retweet lang:en', "tag": "on_deck"},
    {"value": '"slated to pinch hit" -is:retweet lang:en',  "tag": "slated"},
    {"value": '"will pinch hit" -is:retweet lang:en',       "tag": "will_ph"},
]

# ── STATE ─────────────────────────────────────────────────────────────────────
seen_tweet_ids     = set()
posted_alert_keys  = set()
last_reset_date    = None
player_team_map    = {}
last_roster_refresh = 0
player_alert_count = {}
player_alert_time  = {}

TWITTER_HEADERS = {
    "Authorization":  f"Bearer {TWITTER_BEARER_TOKEN}",
    "Content-Type":   "application/json",
}

# ── GAME HOURS ────────────────────────────────────────────────────────────────
def is_game_hours():
    hour = datetime.now(ET_TZ).hour
    return hour >= 12 or hour == 0

# ── DAILY RESET ───────────────────────────────────────────────────────────────
def maybe_reset_daily():
    global seen_tweet_ids, last_reset_date, posted_alert_keys
    global player_alert_count, player_alert_time
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print("[reset] New day — clearing state")
        seen_tweet_ids     = set()
        posted_alert_keys  = set()
        player_alert_count = {}
        player_alert_time  = {}
        last_reset_date    = today

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
    print(f"  📊 {key} alert count: {player_alert_count[key]}")

# ── ROSTER LOOKUP ─────────────────────────────────────────────────────────────
def build_player_team_map():
    global player_team_map, last_roster_refresh
    now = time.time()
    if now - last_roster_refresh < 21600 and player_team_map:
        return
    print("[roster] Refreshing MLB roster...")
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
                full_low = full_name.lower()
                new_map[full_low] = team_name
                if len(parts) >= 2:
                    new_map[parts[0].lower() + " " + parts[-1].lower()] = team_name
            time.sleep(0.2)
        except Exception as e:
            print(f"[roster error] {team_name}: {e}")
    player_team_map     = new_map
    last_roster_refresh = now
    print(f"[roster] {len(new_map)} entries loaded\n")

def is_mlb_player(name):
    """Requires full name (first + last) to match MLB roster."""
    if not name or not player_team_map:
        return False
    nl = name.lower().strip()
    return nl in player_team_map and " " in nl

def lookup_player_team(name):
    if not name or not player_team_map:
        return None
    nl = name.lower().strip()
    return player_team_map.get(nl)

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
    return None

# ── DETECTION ─────────────────────────────────────────────────────────────────
def strip_mentions(text):
    return re.sub(r'@\w+', '', text)

def is_valid_tweet(text):
    """
    Returns (True, reason) if tweet should fire an alert.
    Returns (False, reason) if it should be rejected.
    """
    tl = text.lower()

    # Reject opinion/past/college/hypothetical language
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return False, f"rejected phrase: '{phrase}'"

    # Must have present/future tense marker
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
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:has\s+)?left\s+the\s+game.*?([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch',
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
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:getting\s+)?pinch[- ]hit\s+for',
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
    print(f"  🟢 Reporter: {team} — {summary}")

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
    print(f"  🟡 General: {team} — {summary}")

# ── CORE PROCESSING ───────────────────────────────────────────────────────────
def handle_tweet(tid, text, handle):
    """Process a single tweet and fire alert if valid."""
    maybe_reset_daily()

    # Game hours check
    if not is_game_hours():
        return

    # Deduplicate
    if tid in seen_tweet_ids:
        return
    seen_tweet_ids.add(tid)

    # Core phrase check
    tl = text.lower()
    core_phrases = [
        "on deck to pinch hit", "slated to pinch hit",
        "pinch hit for", "pinch-hit for", "will pinch hit",
        "getting pinch hit", "pinch hitting for",
    ]
    if not any(phrase in tl for phrase in core_phrases):
        return

    # Opinion / hypothetical / college filter
    valid, reason = is_valid_tweet(text)
    if not valid:
        print(f"  🚫 @{handle}: {reason} — {text[:60]}")
        return

    # Extract player names
    pinch_hitter, replaced = extract_players(text)

    # ── CRITICAL: require at least one player name ────────────────────────────
    if not pinch_hitter and not replaced:
        print(f"  🚫 @{handle}: no player names found — skipping")
        return

    # ── MLB roster check — full name required ─────────────────────────────────
    ph_is_mlb  = is_mlb_player(pinch_hitter) if pinch_hitter else False
    rep_is_mlb = is_mlb_player(replaced)     if replaced     else False

    if not ph_is_mlb and not rep_is_mlb:
        print(f"  🚫 @{handle}: '{pinch_hitter}' / '{replaced}' not on MLB roster")
        return

    # Player cooldown
    key_player = pinch_hitter or replaced
    if is_player_on_cooldown(key_player):
        print(f"  🔇 '{key_player}' on cooldown")
        return

    # Determine team
    is_reporter = handle in REPORTER_HANDLES
    reporter    = REPORTER_BY_HANDLE.get(handle)
    team        = reporter["team"] if reporter else None

    if not team:
        team = lookup_player_team(pinch_hitter) or lookup_player_team(replaced)
    if not team:
        team = infer_team_from_text(text)
    if not team:
        team = "Unknown Team"

    url       = f"https://twitter.com/{handle}/status/{tid}"
    alert_key = tid

    if alert_key in posted_alert_keys:
        return
    posted_alert_keys.add(alert_key)

    print(f"  ✅ VALID: @{handle} ({'reporter' if is_reporter else 'general'}) "
          f"team={team} ph={pinch_hitter} out={replaced}")
    print(f"     {text[:120]}")

    record_player_alert(key_player)
    lines_data = get_player_lines(pinch_hitter) if pinch_hitter else {}

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
        print(f"[stream] Deleted {len(rule_ids)} old rules")
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
        print(f"[stream] Added {len(data.get('data', []))} rules")
        if data.get("errors"):
            print(f"[stream rules errors] {data['errors']}")
    except Exception as e:
        print(f"[stream rules add error] {e}")

def setup_stream_rules():
    """Clear existing rules and add our rules."""
    existing = get_stream_rules()
    if existing:
        delete_stream_rules([r["id"] for r in existing])
    time.sleep(1)
    add_stream_rules()

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
    except:
        return []

def connect_stream():
    """Connect to filtered stream and yield tweet data."""
    url = "https://api.twitter.com/2/tweets/search/stream"
    params = {
        "tweet.fields": "created_at,author_id,text",
        "expansions":   "author_id",
        "user.fields":  "username",
    }
    print("[stream] Connecting to filtered stream...")
    r = requests.get(url, headers=TWITTER_HEADERS, params=params, stream=True, timeout=30)
    if r.status_code != 200:
        print(f"[stream error] HTTP {r.status_code}: {r.text[:200]}")
        return
    print("[stream] Connected! Listening for tweets...\n")
    for line in r.iter_lines():
        if line:
            yield line

def run_stream(user_ids):
    """Main stream loop with auto-reconnect."""
    reconnect_wait = 5
    while True:
        try:
            for raw_line in connect_stream():
                reconnect_wait = 5  # reset on successful data
                maybe_reset_daily()
                build_player_team_map()

                # Poll reporter timelines every ~60 seconds via a side check
                # (stream handles general Twitter, we still want reporters)
                try:
                    data = json.loads(raw_line)
                except:
                    continue

                tweet_data = data.get("data", {})
                includes   = data.get("includes", {})
                users      = {u["id"]: u["username"].lower()
                              for u in includes.get("users", [])}

                tid    = tweet_data.get("id", "")
                text   = tweet_data.get("text", "")
                aid    = tweet_data.get("author_id", "")
                handle = users.get(aid, "unknown")

                handle_tweet(tid, text, handle)

        except requests.exceptions.ChunkedEncodingError:
            print(f"[stream] Connection dropped — reconnecting in {reconnect_wait}s...")
        except requests.exceptions.ConnectionError:
            print(f"[stream] Connection error — reconnecting in {reconnect_wait}s...")
        except Exception as e:
            print(f"[stream] Error: {e} — reconnecting in {reconnect_wait}s...")

        time.sleep(reconnect_wait)
        reconnect_wait = min(reconnect_wait * 2, 60)  # exponential backoff, max 60s

# ── REPORTER POLLER ───────────────────────────────────────────────────────────
def poll_reporters_forever(user_ids):
    """
    Runs in background — polls all reporter timelines every 30 seconds.
    Catches reporter tweets that might not match our stream filter keywords.
    """
    import threading

    def loop():
        while True:
            if is_game_hours():
                for reporter in REPORTERS:
                    handle = reporter["handle"].lower()
                    uid    = user_ids.get(handle)
                    if not uid:
                        continue
                    tweets = get_user_tweets(uid, max_results=3)
                    for t in tweets:
                        handle_tweet(
                            t.get("id", ""),
                            t.get("text", ""),
                            handle
                        )
                    time.sleep(0.5)
            time.sleep(30)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    print("[reporters] Background reporter polling started (every 30s)\n")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot v5 — Streaming Edition")
    print("   Real-time Twitter Filtered Stream (no more polling delay)")
    print("   REQUIRE player names — no name = no alert")
    print("   Tighter opinion/hypothetical/college filters")
    print(f"   {len(REPORTERS)} reporters monitored in background\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return
    if not DISCORD_WEBHOOK_URL:
        print("[error] PINCH_HIT_WEBHOOK_URL not set!")
        return

    # Build roster
    build_player_team_map()

    # Get reporter user IDs
    print("Looking up reporter user IDs...")
    handles  = [r["handle"] for r in REPORTERS]
    user_ids = {}
    for i in range(0, len(handles), 100):
        user_ids.update(get_user_ids_batch(handles[i:i+100]))
    print(f"Found {len(user_ids)} user IDs\n")

    # Set up stream filter rules
    setup_stream_rules()

    # Start reporter background poller
    poll_reporters_forever(user_ids)

    # Start main stream (blocks forever, auto-reconnects)
    run_stream(user_ids)

if __name__ == "__main__":
    run()
