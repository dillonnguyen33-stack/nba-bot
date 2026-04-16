"""
MLB Pinch Hit Alert Bot
Monitors beat reporters + general Twitter for pinch hit keywords
Posts alerts to Discord with player lines from DK, FD, Fliff, Hard Rock, Bet365
Requires 3+ sources within 3 minute window to fire alert
"""

import os
import re
import time
import requests
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN  = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL   = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY          = os.environ.get("ODDS_API_KEY")
POLL_INTERVAL         = 45    # seconds between checks
ALERT_WINDOW          = 180   # seconds — 3 minute window
MIN_SOURCES           = 3     # minimum sources to fire alert

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

REPORTER_HANDLES    = {r["handle"].lower() for r in REPORTERS}
REPORTER_BY_HANDLE  = {r["handle"].lower(): r for r in REPORTERS}

# ── KEYWORDS ──────────────────────────────────────────────────────────────────
PINCH_HIT_KEYWORDS = [
    "pinch hit",
    "pinch hitting",
    "pinch hitter",
    "pinch-hit",
    "on deck for",
    "batting for",
    "will bat for",
    "coming out for",
    "being lifted for",
    "ph for",
]

# Top keywords for search queries (shorter = safer)
SEARCH_KEYWORDS = [
    "pinch hit",
    "pinch-hit",
    "batting for",
    "ph for",
]

# ── ODDS API ──────────────────────────────────────────────────────────────────
PROP_BOOKS = {
    "draftkings":  "DraftKings",
    "fanduel":     "FanDuel",
    "fliff":       "Fliff",
    "hardrockbet": "Hard Rock",
    "bet365":      "Bet365",
}

MLB_PROP_MARKETS = [
    "batter_hits",
    "batter_total_bases",
    "batter_rbis",
    "batter_home_runs",
]

# ── STATE ─────────────────────────────────────────────────────────────────────
recent_signals  = {}
seen_tweet_ids  = set()
posted_alerts   = set()

# ── TWITTER ───────────────────────────────────────────────────────────────────
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

def search_tweets(query, max_results=10):
    """Search recent tweets — keep query short to avoid 400 errors."""
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
            print(f"[twitter 400] Query too long or invalid: {query[:80]}...")
            return {}
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[twitter error] {e}")
        return {}

def build_reporter_batches():
    """Split reporters into small batches of 10 to keep queries short."""
    handles = [r["handle"] for r in REPORTERS]
    batches = []
    for i in range(0, len(handles), 10):
        batches.append(handles[i:i+10])
    return batches

# ── KEYWORD DETECTION ─────────────────────────────────────────────────────────
def contains_keyword(text):
    tl = text.lower()
    return any(kw in tl for kw in PINCH_HIT_KEYWORDS)

def matched_keyword(text):
    tl = text.lower()
    for kw in PINCH_HIT_KEYWORDS:
        if kw in tl:
            return kw
    return ""

def extract_player(text):
    patterns = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?batting\s+for',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?on\s+deck\s+for',
        r'(?:ph|pinch[- ]hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?coming\s+out',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:being\s+)?lifted',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

def find_common_player(signals):
    players = [s["player"] for s in signals if s["player"]]
    if not players:
        return None
    counts = {}
    for p in players:
        counts[p.lower()] = counts.get(p.lower(), 0) + 1
    best = max(counts, key=counts.get)
    return best.title() if counts[best] >= 1 else None

# ── ODDS API ──────────────────────────────────────────────────────────────────
def get_player_lines(player_name):
    if not ODDS_API_KEY or not player_name:
        return {}

    last_name = player_name.split()[-1].lower()
    results   = {}

    try:
        events = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=10
        ).json()
    except Exception as e:
        print(f"[odds error] {e}")
        return {}

    for event in events[:8]:
        event_id = event.get("id")
        for market in MLB_PROP_MARKETS:
            try:
                r = requests.get(
                    f"https://api.the-odds-api.com/v4/sports/baseball_mlb/events/{event_id}/odds",
                    params={
                        "apiKey":     ODDS_API_KEY,
                        "markets":    market,
                        "bookmakers": ",".join(PROP_BOOKS.keys()),
                        "oddsFormat": "american",
                        "regions":    "us,us2",
                    },
                    timeout=10
                ).json()
            except:
                continue

            for bookmaker in r.get("bookmakers", []):
                bkey  = bookmaker.get("key", "")
                bname = PROP_BOOKS.get(bkey)
                if not bname:
                    continue
                for mkt in bookmaker.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        desc  = outcome.get("description", "").lower()
                        if last_name not in desc:
                            continue
                        if outcome.get("name") != "Under":
                            continue
                        point = outcome.get("point")
                        price = outcome.get("price")
                        if point is None or price is None:
                            continue
                        label = market.replace("batter_", "").replace("_", " ").title()
                        key   = f"{bname}_{label}"
                        if key not in results:
                            results[key] = {"book": bname, "market": label,
                                            "line": point, "under": price}
    return results

def format_lines(lines_data):
    if not lines_data:
        return "📋 Lines: not found on tracked books"
    by_market = {}
    for data in lines_data.values():
        mkt = data["market"]
        if mkt not in by_market:
            by_market[mkt] = []
        odds_str = f"+{data['under']}" if data["under"] > 0 else str(data["under"])
        by_market[mkt].append(f"{data['book']}: u{data['line']} ({odds_str})")
    lines = ["📋 **Player lines — BET UNDER:**"]
    for market, entries in by_market.items():
        lines.append(f"**{market}:** " + " | ".join(entries))
    return "\n".join(lines)

# ── DISCORD ───────────────────────────────────────────────────────────────────
def post_alert(team, signals, player_name, lines_data):
    if not DISCORD_WEBHOOK_URL:
        print(f"[discord] No webhook — would post: {team} {player_name}")
        return

    num_sources    = len(signals)
    reporter_count = sum(1 for s in signals if s["is_reporter"])
    general_count  = num_sources - reporter_count

    source_lines = []
    for s in signals[:4]:
        label = "🎙️ Beat reporter" if s["is_reporter"] else "🌐 Twitter"
        source_lines.append(
            f"{label} **@{s['handle']}:** _{s['text'][:100]}_\n🔗 [Tweet]({s['url']})"
        )

    player_line  = f"\n👤 **Player:** {player_name}" if player_name else ""
    sources_text = "\n\n".join(source_lines)
    lines_text   = format_lines(lines_data)

    embed = {
        "embeds": [{
            "title": f"⚾🚨 PINCH HIT ALERT — {team}",
            "description": (
                f"**{num_sources} sources confirmed** "
                f"({reporter_count} beat reporters + {general_count} general)\n"
                f"{player_line}\n\n"
                f"{sources_text}\n\n"
                f"{lines_text}\n\n"
                f"💰 **BET THE UNDER ON ALL LINES NOW**"
            ),
            "color": 0x00FF00,
            "footer": {"text": f"Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}
        }]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        r.raise_for_status()
        print(f"  ✅ Alert posted: {team} — {num_sources} sources — {player_name}")
    except Exception as e:
        print(f"[discord error] {e}")

# ── PROCESS TWEETS ────────────────────────────────────────────────────────────
def process_response(data):
    tweets = data.get("data", [])
    users  = {u["id"]: u["username"].lower()
              for u in data.get("includes", {}).get("users", [])}
    now    = datetime.now(timezone.utc).timestamp()
    new_signals = []

    for tweet in tweets:
        tweet_id   = tweet["id"]
        tweet_text = tweet.get("text", "")
        author_id  = tweet.get("author_id", "")
        handle     = users.get(author_id, "unknown")

        if tweet_id in seen_tweet_ids:
            continue
        seen_tweet_ids.add(tweet_id)

        if not contains_keyword(tweet_text):
            continue

        is_reporter = handle in REPORTER_HANDLES
        reporter    = REPORTER_BY_HANDLE.get(handle)
        team        = reporter["team"] if reporter else None
        player      = extract_player(tweet_text)
        tweet_url   = f"https://twitter.com/{handle}/status/{tweet_id}"

        new_signals.append({
            "handle":      handle,
            "text":        tweet_text,
            "url":         tweet_url,
            "player":      player,
            "team":        team,
            "is_reporter": is_reporter,
            "timestamp":   now,
        })

        print(f"  📡 Signal: @{handle} ({'reporter' if is_reporter else 'general'}) "
              f"team={team} player={player}")

    return new_signals

def check_and_alert():
    now = datetime.now(timezone.utc).timestamp()
    for team in list(recent_signals.keys()):
        recent_signals[team] = [
            s for s in recent_signals[team]
            if now - s["timestamp"] <= ALERT_WINDOW
        ]
        active = recent_signals[team]
        if len(active) < MIN_SOURCES:
            continue

        player      = find_common_player(active)
        time_bucket = int(now / ALERT_WINDOW)
        alert_key   = f"{team}_{player}_{time_bucket}"

        if alert_key in posted_alerts:
            continue
        posted_alerts.add(alert_key)

        lines_data = get_player_lines(player) if player else {}
        post_alert(team, active, player, lines_data)

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def run():
    print("⚾ MLB Pinch Hit Bot started!")
    print(f"   Monitoring {len(REPORTERS)} beat reporters")
    print(f"   Alert threshold: {MIN_SOURCES}+ sources within {ALERT_WINDOW}s")
    print(f"   Polling every {POLL_INTERVAL}s\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return

    reporter_batches = build_reporter_batches()
    print(f"   Reporter batches: {len(reporter_batches)} groups of ~10\n")

    # Build keyword part (short)
    kw_part = " OR ".join([f'"{kw}"' for kw in SEARCH_KEYWORDS])

    cycle = 0

    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning Twitter... (cycle {cycle})")

        all_new = []

        # Search each reporter batch separately to keep queries short
        for batch in reporter_batches:
            handles_part = " OR ".join([f"from:{h}" for h in batch])
            query = f"({kw_part}) ({handles_part}) -is:retweet lang:en"
            data  = search_tweets(query, max_results=10)
            signals = process_response(data)
            all_new.extend(signals)
            time.sleep(1)  # small delay between batch requests

        # General Twitter search (no from: filter — catches non-reporter accounts)
        general_query = f"({kw_part}) baseball mlb -is:retweet lang:en"
        general_data  = search_tweets(general_query, max_results=15)
        general_sigs  = process_response(general_data)
        all_new.extend(general_sigs)

        # Bucket signals by team
        for signal in all_new:
            team = signal["team"]
            if not team:
                # Try to infer team from tweet text
                text_lower = signal["text"].lower()
                for r in REPORTERS:
                    if r["team"].lower() in text_lower:
                        team = r["team"]
                        signal["team"] = team
                        break
            if not team:
                continue

            if team not in recent_signals:
                recent_signals[team] = []
            recent_signals[team].append(signal)

        check_and_alert()
        cycle += 1
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
