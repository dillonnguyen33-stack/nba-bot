"""
MLB Pinch Hit Alert Bot - Final Version
Two tier system:
  TIER 1: 2+ general Twitter sources with strict pre-event pinch hit language
  TIER 2: 1+ verified beat reporter confirms → mega confirmation message

Key design decisions:
  - Strict regex patterns only — no vague keywords like "on deck" or "coming out"
  - Reject phrases tuned to not block legitimate tweets
  - Roster lookup maps player names to teams automatically
  - Game hours 12pm-1am ET only
  - Daily reset at midnight
  - @everyone on every alert
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
POLL_INTERVAL        = 30       # seconds between scans
ALERT_WINDOW         = 180      # 3 minute window for cross referencing
ET_TZ                = pytz.timezone("America/New_York")
TIER1_MIN_GENERAL    = 2        # general Twitter sources needed for Tier 1
TIER2_MIN_REPORTERS  = 1        # beat reporters needed for Tier 2

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

# ── TEAM ALIASES ──────────────────────────────────────────────────────────────
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

# ── PRE-EVENT PATTERNS ────────────────────────────────────────────────────────
# These are tight and specific — only match genuine pre-event pinch hit language
PRE_EVENT_PATTERNS = [
    r'\bwill\s+pinch[- ]hit\b',            # "X will pinch hit"
    r'\bpinch[- ]hitting\s+for\b',         # "pinch hitting for X"
    r'\bpinch[- ]hit\s+for\b',             # "pinch hit for X"
    r'\bis\s+pinch[- ]hitting\b',          # "X is pinch hitting"
    r'\bph\s+for\b',                       # "ph for X" — baseball shorthand
    r'\bpinch\s+hitter\s+(?:is\s+)?up\b',  # "pinch hitter up"
    r'\bsent\s+up\s+to\s+bat\b',           # "sent up to bat"
    r'\bsent\s+to\s+the\s+plate\b',        # "sent to the plate"
    r'\bup\s+to\s+bat\s+for\b',            # "up to bat for X"
    r'\bgoing\s+to\s+pinch[- ]hit\b',      # "going to pinch hit"
    r'\bwill\s+bat\s+for\b',               # "will bat for X"
    r'\bpinch[- ]hitting\b',               # "X is pinch-hitting"
]

# ── REJECT PHRASES ────────────────────────────────────────────────────────────
# These indicate the event ALREADY happened — reject these tweets
REJECT_PHRASES = [
    "home run", "homered", "hit a", "singled", "doubled", "tripled",
    "drove in", "rbi", "scores", "scored", "flies out", "grounds out",
    "struck out", "strikeout", "career", "first career",
    "solo shot", "connects", "connected", "reaches", "reached",
    "pops out", "lines out",
    "pinch-hit home run", "pinch hit home run",
    "pinch-hit single", "pinch hit single",
    "pinch-hit rbi", "pinch hit rbi",
    "just hit", "just pinch hit", "already pinch",
    "last night", "yesterday",
]

# ── ODDS API ──────────────────────────────────────────────────────────────────
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
recent_signals      = {}
seen_tweet_ids      = set()
tier1_posted        = set()
tier2_posted        = set()
last_reset_date     = None
player_team_map     = {}
last_roster_refresh = 0

TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

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
    global seen_tweet_ids, last_reset_date, recent_signals, tier1_posted, tier2_posted
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print("[reset] New day — clearing all state")
        seen_tweet_ids  = set()
        recent_signals  = {}
        tier1_posted    = set()
        tier2_posted    = set()
        last_reset_date = today

# ── MLB LINEUP CHECK ──────────────────────────────────────────────────────────
def get_live_game_ids():
    try:
        r = requests.get(
            "https://statsapi.mlb.com/api/v1/schedule",
            params={"sportId": 1, "gameType": "R",
                    "fields": "dates,games,gamePk,status,abstractGameState"},
            timeout=10
        )
        r.raise_for_status()
        return [
            g["gamePk"]
            for d in r.json().get("dates", [])
            for g in d.get("games", [])
            if g.get("status", {}).get("abstractGameState") == "Live"
        ]
    except:
        return []

def get_starting_lineup(game_pk):
    try:
        r = requests.get(
            f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live",
            timeout=10
        )
        r.raise_for_status()
        starters = set()
        for side in ["home", "away"]:
            for pid, pd in r.json().get("liveData", {}).get("boxscore", {}).get(
                    "teams", {}).get(side, {}).get("players", {}).items():
                bo = pd.get("battingOrder", "")
                if bo and str(bo).endswith("0"):
                    name = pd.get("person", {}).get("fullName", "").lower()
                    if name:
                        starters.add(name)
        return starters
    except:
        return set()

def verify_in_lineup(player_name):
    if not player_name:
        return None
    game_ids = get_live_game_ids()
    if not game_ids:
        return None
    last = player_name.lower().split()[-1]
    for gid in game_ids[:6]:
        for starter in get_starting_lineup(gid):
            if last in starter:
                return True
    return False

# ── CONFIDENCE SCORE ──────────────────────────────────────────────────────────
def calculate_confidence(signals, lineup_verified):
    score     = 0
    num       = len(signals)
    reporters = sum(1 for s in signals if s["is_reporter"])

    if num >= 5:   score += 5
    elif num >= 4: score += 4
    elif num >= 3: score += 3
    elif num >= 2: score += 2
    else:          score += 1

    if reporters >= 3:   score += 3
    elif reporters >= 2: score += 2
    elif reporters >= 1: score += 1

    if lineup_verified is True:   score += 2
    elif lineup_verified is None: score += 1

    return min(score, 10)

def confidence_emoji(score):
    if score >= 8: return "🟢"
    if score >= 6: return "🟡"
    return "🔴"

# ── TWITTER ───────────────────────────────────────────────────────────────────
def search_tweets(query, max_results=10):
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=TWITTER_HEADERS,
            params={
                "query": query, "max_results": max_results,
                "tweet.fields": "created_at,author_id,text",
                "expansions": "author_id", "user.fields": "username",
            },
            timeout=15
        )
        if r.status_code == 400:
            print(f"[twitter 400] {query[:60]}...")
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[twitter error] {e}")
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

# ── DETECTION ─────────────────────────────────────────────────────────────────
def is_pre_event(text):
    """
    Returns True only if:
    1. Tweet matches a strict pre-event pinch hit pattern
    2. Tweet does NOT contain any result/outcome language
    """
    tl = text.lower()

    # Step 1 — reject if result language found
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return False

    # Step 2 — must match at least one strict pre-event pattern
    for pattern in PRE_EVENT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True

    return False

def extract_players(text):
    """Extract (pinch_hitter, replaced_player) from tweet text."""
    # Both players mentioned
    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?batting\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?bat\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, text)
        if m:
            return m.group(1), m.group(2)

    # Only pinch hitter mentioned
    patterns_hitter = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'(?:ph|pinch[- ]hit(?:ting)?)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+sent\s+(?:up\s+)?to\s+(?:the\s+)?(?:bat|plate)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?pinch[- ]hitting',
    ]
    for p in patterns_hitter:
        m = re.search(p, text)
        if m:
            return m.group(1), None

    # Only replaced player mentioned
    patterns_out = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:being\s+)?lifted',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?coming\s+out',
    ]
    for p in patterns_out:
        m = re.search(p, text)
        if m:
            return None, m.group(1)

    return None, None

def build_summary(signals):
    pinch_hitters, replaced = [], []
    for s in signals:
        ph, rep = s.get("pinch_hitter"), s.get("replaced")
        if ph and ph not in pinch_hitters:
            pinch_hitters.append(ph)
        if rep and rep not in replaced:
            replaced.append(rep)
    if pinch_hitters and replaced:
        return f"**{pinch_hitters[0]}** will pinch hit for **{replaced[0]}**"
    elif pinch_hitters:
        return f"**{pinch_hitters[0]}** is being called to pinch hit"
    elif replaced:
        return f"**{replaced[0]}** is coming out — pinch hitter incoming"
    return "Pinch hit situation detected"

def find_most_common(signals, key):
    players = [s[key] for s in signals if s.get(key)]
    if not players:
        return None
    counts = {}
    for p in players:
        counts[p.lower()] = counts.get(p.lower(), 0) + 1
    return max(counts, key=counts.get).title()

# ── ODDS ──────────────────────────────────────────────────────────────────────
def get_player_lines(player_name):
    if not ODDS_API_KEY or not player_name:
        return {}
    last_name = player_name.split()[-1].lower()
    results = {}
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
                        key = f"{bname}_{label}"
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
        print("[discord error] Webhook URL is missing!")
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"[discord error] {e}")

def post_tier1(team, signals, lines_data, confidence, lineup_verified):
    summary = build_summary(signals)
    conf_em = confidence_emoji(confidence)
    gc      = sum(1 for s in signals if not s["is_reporter"])

    if lineup_verified is True:
        lineup_note = "✅ Starter confirmed in lineup"
    elif lineup_verified is False:
        lineup_note = "⚠️ Not in starting lineup — possible injury sub"
    else:
        lineup_note = "❓ Lineup check unavailable"

    seen_handles, src_lines = set(), []
    for s in signals:
        if s["handle"] in seen_handles:
            continue
        seen_handles.add(s["handle"])
        label = "🎙️ Reporter" if s["is_reporter"] else "🌐 Twitter"
        src_lines.append(
            f"{label} **@{s['handle']}:** _{s['text'][:100]}_\n🔗 [Tweet]({s['url']})"
        )
        if len(src_lines) >= 3:
            break

    embed = {"embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
        "description": (
            f"**{gc} Twitter sources** reporting pre-event pinch hit\n"
            f"{conf_em} **Confidence: {confidence}/10** | {lineup_note}\n\n"
            f"📋 **{summary}**\n\n"
            + "\n\n".join(src_lines) +
            f"\n\n{format_lines(lines_data)}\n\n"
            f"💰 **BET THE UNDER ON ALL LINES NOW**\n"
            f"_Awaiting beat reporter confirmation..._"
        ),
        "color": 0xF1C40F,
        "footer": {"text": f"Tier 1 · Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}}]}

    post_discord({"content": "@everyone", "embeds": embed["embeds"]})
    print(f"  🟡 Tier 1: {team} — {summary} (conf={confidence})")

def post_tier2(team, reporter_signals, all_signals, lines_data):
    summary   = build_summary(all_signals)
    rep_lines = []
    seen      = set()
    for s in reporter_signals:
        if s["handle"] in seen:
            continue
        seen.add(s["handle"])
        rep_lines.append(
            f"🎙️ **@{s['handle']}:** _{s['text'][:100]}_\n🔗 [Tweet]({s['url']})"
        )
        if len(rep_lines) >= 2:
            break

    embed = {"embeds": [{"title": f"🔥💥 BEAT REPORTER CONFIRMED — {team}",
        "description": (
            f"**MEGA CONFIRMATION — Verified beat reporter confirmed the pinch hit**\n\n"
            f"📋 **{summary}**\n\n"
            + "\n\n".join(rep_lines) +
            f"\n\n{format_lines(lines_data)}\n\n"
            f"💰 **HIGH CONFIDENCE — BET THE UNDER NOW**"
        ),
        "color": 0x00FF00,
        "footer": {"text": f"Tier 2 · Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}}]}

    post_discord({"content": "@everyone 🔥 REPORTER CONFIRMED", "embeds": embed["embeds"]})
    print(f"  🟢 Tier 2: {team} — {summary}")

# ── PROCESS TWEETS ────────────────────────────────────────────────────────────
def process_tweets(tweets, users):
    now        = datetime.now(timezone.utc).timestamp()
    new_signals = []

    for tweet in tweets:
        tid    = tweet.get("id") or tweet.get("tweet_id", "")
        text   = tweet.get("text", "")
        aid    = tweet.get("author_id", "")
        handle = users.get(aid, tweet.get("handle", "unknown")).lower()

        # Deduplicate
        if tid in seen_tweet_ids:
            continue
        seen_tweet_ids.add(tid)

        # Strict pre-event check
        if not is_pre_event(text):
            continue

        is_reporter            = handle in REPORTER_HANDLES
        reporter               = REPORTER_BY_HANDLE.get(handle)
        team                   = reporter["team"] if reporter else None
        pinch_hitter, replaced = extract_players(text)
        url                    = f"https://twitter.com/{handle}/status/{tid}"

        # Team inference
        if not team:
            if pinch_hitter:
                team = lookup_player_team(pinch_hitter)
            if not team and replaced:
                team = lookup_player_team(replaced)
            if not team:
                team = infer_team_from_text(text)

        if not team:
            print(f"  ❓ No team: @{handle}: {text[:60]}")
            continue

        new_signals.append({
            "handle":       handle,
            "text":         text,
            "url":          url,
            "pinch_hitter": pinch_hitter,
            "replaced":     replaced,
            "team":         team,
            "is_reporter":  is_reporter,
            "timestamp":    now,
        })
        print(f"  📡 @{handle} ({'rep' if is_reporter else 'gen'}) "
              f"team={team} ph={pinch_hitter} out={replaced}")

    return new_signals

# ── CHECK AND ALERT ───────────────────────────────────────────────────────────
def check_and_alert():
    now = datetime.now(timezone.utc).timestamp()

    for team in list(recent_signals.keys()):
        # Remove stale signals
        recent_signals[team] = [
            s for s in recent_signals[team]
            if now - s["timestamp"] <= ALERT_WINDOW
        ]
        active = recent_signals[team]
        if not active:
            continue

        general_signals  = [s for s in active if not s["is_reporter"]]
        reporter_signals = [s for s in active if s["is_reporter"]]
        time_bucket      = int(now / ALERT_WINDOW)
        tier1_key        = f"t1_{team}_{time_bucket}"
        tier2_key        = f"t2_{team}_{time_bucket}"
        pinch_hitter     = find_most_common(active, "pinch_hitter")
        replaced         = find_most_common(active, "replaced")

        # ── TIER 2 FIRST: reporter can bypass Tier 1 requirement ─────────────
        if len(reporter_signals) >= TIER2_MIN_REPORTERS and tier2_key not in tier2_posted:
            tier2_posted.add(tier2_key)
            tier1_posted.add(tier1_key)  # prevent redundant Tier 1 after Tier 2
            lines_data = get_player_lines(pinch_hitter) if pinch_hitter else {}
            post_tier2(team, reporter_signals, active, lines_data)

        # ── TIER 1: 2+ general Twitter sources (only if Tier 2 hasn't fired) ─
        elif len(general_signals) >= TIER1_MIN_GENERAL and tier1_key not in tier1_posted:
            tier1_posted.add(tier1_key)
            lineup_verified = verify_in_lineup(replaced)
            confidence      = calculate_confidence(active, lineup_verified)
            lines_data      = get_player_lines(pinch_hitter) if pinch_hitter else {}
            post_tier1(team, active, lines_data, confidence, lineup_verified)

def add_signals(new_signals):
    for s in new_signals:
        team = s.get("team")
        if not team:
            continue
        recent_signals.setdefault(team, []).append(s)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot — Final Version")
    print(f"   Tier 1: {TIER1_MIN_GENERAL}+ general Twitter sources")
    print(f"   Tier 2: {TIER2_MIN_REPORTERS}+ beat reporter confirmation")
    print(f"   {len(REPORTERS)} reporters | window={ALERT_WINDOW}s | poll={POLL_INTERVAL}s\n")

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
        all_new = []

        # General Twitter keyword searches
        for kw in ["pinch hit", "pinch-hit"]:
            query = f'"{kw}" baseball -is:retweet lang:en'
            data  = search_tweets(query, max_results=15)
            tweets = data.get("data", [])
            users  = {u["id"]: u["username"] for u in
                      data.get("includes", {}).get("users", [])}
            all_new.extend(process_tweets(tweets, users))
            time.sleep(2)

        # Rotate through reporter timelines (6 per cycle)
        batch_start     = (cycle * 6) % len(REPORTERS)
        batch_reporters = REPORTERS[batch_start:batch_start + 6]
        for reporter in batch_reporters:
            handle = reporter["handle"].lower()
            uid    = user_ids.get(handle)
            if not uid:
                continue
            tweets = get_user_tweets(uid, max_results=3)
            for t in tweets:
                t["author_id"] = uid
            all_new.extend(process_tweets(tweets, {uid: reporter["handle"]}))
            time.sleep(1)

        add_signals(all_new)
        check_and_alert()
        cycle += 1
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
