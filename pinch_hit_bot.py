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
POLL_INTERVAL         = 30    # seconds between checks
ALERT_WINDOW          = 180   # seconds — 3 minute window for cross referencing
MIN_SOURCES           = 3     # minimum sources needed to fire alert

# ── BEAT REPORTERS ────────────────────────────────────────────────────────────
REPORTERS = [
    # AL East
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
    # AL Central
    {"handle": "scottmerkin",     "team": "White Sox"},
    {"handle": "JRFegan",         "team": "White Sox"},
    {"handle": "ZackMeisel",      "team": "Guardians"},
    {"handle": "beckjason",       "team": "Tigers"},
    {"handle": "CodyStavenhagen", "team": "Tigers"},
    {"handle": "alec_lewis",      "team": "Royals"},
    {"handle": "DanHayesMLB",     "team": "Twins"},
    {"handle": "dohyoungpark",    "team": "Twins"},
    # AL West
    {"handle": "brianmctaggart",  "team": "Astros"},
    {"handle": "Chandler_Rome",   "team": "Astros"},
    {"handle": "RhettBollinger",  "team": "Angels"},
    {"handle": "JeffFletcherOCR", "team": "Angels"},
    {"handle": "MartinJGallegos", "team": "Athletics"},
    {"handle": "DKramer_",        "team": "Mariners"},
    {"handle": "RyanDivish",      "team": "Mariners"},
    {"handle": "kennlandry",      "team": "Rangers"},
    {"handle": "Evan_P_Grant",    "team": "Rangers"},
    # NL East
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
    # NL Central
    {"handle": "MLBastian",       "team": "Cubs"},
    {"handle": "sahadevsharma",   "team": "Cubs"},
    {"handle": "m_sheldon",       "team": "Reds"},
    {"handle": "AdamMcCalvy",     "team": "Brewers"},
    {"handle": "Todd_Rosiak",     "team": "Brewers"},
    {"handle": "justdelossantos", "team": "Pirates"},
    {"handle": "katiejwoo",       "team": "Cardinals"},
    {"handle": "JohnDenton555",   "team": "Cardinals"},
    # NL West
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

REPORTER_HANDLES = {r["handle"].lower() for r in REPORTERS}
REPORTER_BY_HANDLE = {r["handle"].lower(): r for r in REPORTERS}

# ── KEYWORDS — tight and specific ─────────────────────────────────────────────
PINCH_HIT_KEYWORDS = [
    "pinch hit",
    "pinch hitting",
    "pinch hitter",
    "pinch-hit",
    "pinch-hitting",
    "pinch-hitter",
    "on deck for",
    "batting for",
    "will bat for",
    "coming out for",
    "being lifted for",
    "lifted for",
    "will pinch",
    "ph for",
    "comes out for",
]

# ── ODDS API BOOKS ────────────────────────────────────────────────────────────
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
    "batter_strikeouts",
    "batter_walks",
]

# ── STATE ─────────────────────────────────────────────────────────────────────
recent_signals   = {}   # {team: [{handle, text, url, player, timestamp, is_reporter}]}
seen_tweet_ids   = set()
posted_alerts    = set()

# ── TWITTER ───────────────────────────────────────────────────────────────────
TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

def search_tweets(query, max_results=20):
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            headers=TWITTER_HEADERS,
            params={
                "query":       query,
                "max_results": max_results,
                "tweet.fields": "created_at,author_id,text",
                "expansions":  "author_id",
                "user.fields": "username",
            },
            timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[twitter error] {e}")
        return {}

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
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+comes\s+out',
    ]
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1)
    return None

def find_common_player(signals):
    """Find if multiple signals mention the same player name."""
    players = [s["player"] for s in signals if s["player"]]
    if not players:
        return None
    # Return most common player name
    counts = {}
    for p in players:
        p_lower = p.lower()
        counts[p_lower] = counts.get(p_lower, 0) + 1
    best = max(counts, key=counts.get)
    if counts[best] >= 2:
        return best.title()
    return players[0] if players else None

# ── ODDS API ──────────────────────────────────────────────────────────────────
def get_player_lines(player_name):
    """Pull MLB player prop lines from DK, FD, Fliff, Hard Rock, Bet365."""
    if not ODDS_API_KEY or not player_name:
        return {}

    last_name = player_name.split()[-1].lower()
    results   = {}

    try:
        events_r = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/events",
            params={"apiKey": ODDS_API_KEY},
            timeout=10
        )
        events_r.raise_for_status()
        events = events_r.json()
    except Exception as e:
        print(f"[odds error] events: {e}")
        return {}

    for event in events[:10]:
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

                        name  = outcome.get("name", "")
                        point = outcome.get("point")
                        price = outcome.get("price")

                        if name == "Under" and point is not None and price is not None:
                            market_label = market.replace("batter_", "").replace("_", " ").title()
                            key = f"{bname}_{market_label}"
                            if key not in results:
                                results[key] = {
                                    "book":    bname,
                                    "market":  market_label,
                                    "line":    point,
                                    "under":   price,
                                }
    return results

def format_lines(lines_data):
    """Format odds lines for Discord message."""
    if not lines_data:
        return "📋 Lines: not found on tracked books"

    # Group by market
    by_market = {}
    for key, data in lines_data.items():
        mkt = data["market"]
        if mkt not in by_market:
            by_market[mkt] = []
        odds_display = f"+{data['under']}" if data["under"] > 0 else str(data["under"])
        by_market[mkt].append(f"{data['book']}: u{data['line']} ({odds_display})")

    lines = ["📋 **Player lines — BET UNDER ON ALL:**"]
    for market, entries in by_market.items():
        lines.append(f"**{market}:** " + " | ".join(entries))
    return "\n".join(lines)

# ── DISCORD ───────────────────────────────────────────────────────────────────
def post_alert(team, signals, player_name, lines_data):
    if not DISCORD_WEBHOOK_URL:
        print("[discord] No webhook set")
        return

    num_sources    = len(signals)
    reporter_count = sum(1 for s in signals if s["is_reporter"])
    general_count  = num_sources - reporter_count

    # Build source list
    source_lines = []
    for s in signals[:5]:  # show max 5 sources
        label = "🎙️ Beat reporter" if s["is_reporter"] else "🌐 Twitter"
        source_lines.append(
            f"{label} **@{s['handle']}:** _{s['text'][:120]}_\n🔗 [Tweet]({s['url']})"
        )

    player_line  = f"\n👤 **Player being pinch hit:** {player_name}" if player_name else ""
    sources_text = "\n\n".join(source_lines)
    lines_text   = format_lines(lines_data)

    embed = {
        "embeds": [{
            "title": f"⚾🚨 PINCH HIT ALERT — {team}",
            "description": (
                f"**{num_sources} sources confirmed** "
                f"({reporter_count} beat reporters + {general_count} general Twitter)\n"
                f"{player_line}\n\n"
                f"{sources_text}\n\n"
                f"{lines_text}\n\n"
                f"💰 **BET THE UNDER ON ALL LINES NOW**"
            ),
            "color": 0x00FF00,
            "footer": {
                "text": f"Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"
            }
        }]
    }

    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10)
        r.raise_for_status()
        print(f"  ✅ Alert posted: {team} — {num_sources} sources — {player_name}")
    except Exception as e:
        print(f"[discord error] {e}")

# ── MAIN LOOP ─────────────────────────────────────────────────────────────────
def build_search_query():
    """Build Twitter search query for beat reporters + general keyword search."""
    reporter_handles = " OR ".join([f"from:{r['handle']}" for r in REPORTERS])
    keywords         = " OR ".join([f'"{kw}"' for kw in PINCH_HIT_KEYWORDS[:6]])

    # Search 1: beat reporters tweeting anything about pinch hits
    reporter_query = f"({keywords}) ({reporter_handles}) -is:retweet lang:en"

    # Search 2: general Twitter search for pinch hit keywords (broader)
    general_query  = f"({keywords}) baseball -is:retweet lang:en"

    return reporter_query, general_query

def process_tweets(data, is_reporter_search):
    """Process tweets from API response."""
    tweets = data.get("data", [])
    users  = {u["id"]: u["username"].lower()
              for u in data.get("includes", {}).get("users", [])}

    now = datetime.now(timezone.utc).timestamp()
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
        keyword     = matched_keyword(tweet_text)

        new_signals.append({
            "handle":      handle,
            "text":        tweet_text,
            "url":         tweet_url,
            "player":      player,
            "team":        team,
            "is_reporter": is_reporter,
            "keyword":     keyword,
            "timestamp":   now,
        })

        print(f"  📡 Signal: @{handle} ({'reporter' if is_reporter else 'general'}) "
              f"— team: {team} — player: {player}")

    return new_signals

def check_and_alert():
    """Check all accumulated signals and fire alerts if threshold met."""
    now = datetime.now(timezone.utc).timestamp()

    for team, signals in list(recent_signals.items()):
        # Remove stale signals outside window
        recent_signals[team] = [
            s for s in signals
            if now - s["timestamp"] <= ALERT_WINDOW
        ]

        active = recent_signals[team]
        if len(active) < MIN_SOURCES:
            continue

        # Create unique alert key based on team + dominant player + time bucket
        player      = find_common_player(active)
        time_bucket = int(now / ALERT_WINDOW)
        alert_key   = f"{team}_{player}_{time_bucket}"

        if alert_key in posted_alerts:
            continue

        posted_alerts.add(alert_key)

        # Pull odds lines
        lines_data = get_player_lines(player) if player else {}

        # Post to Discord
        post_alert(team, active, player, lines_data)

def run():
    print("⚾ MLB Pinch Hit Bot started!")
    print(f"   Monitoring {len(REPORTERS)} beat reporters")
    print(f"   Alert threshold: {MIN_SOURCES}+ sources within {ALERT_WINDOW}s")
    print(f"   Polling every {POLL_INTERVAL}s\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return

    reporter_query, general_query = build_search_query()

    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Scanning Twitter...")

        # Search 1: Beat reporters
        reporter_data = search_tweets(reporter_query, max_results=20)
        reporter_signals = process_tweets(reporter_data, is_reporter_search=True)

        # Search 2: General Twitter (broader catch)
        general_data = search_tweets(general_query, max_results=20)
        general_signals = process_tweets(general_data, is_reporter_search=False)

        all_new = reporter_signals + general_signals

        # Bucket signals by team
        for signal in all_new:
            team = signal["team"]

            # For general signals, try to match team from tweet content
            if not team:
                for r in REPORTERS:
                    if r["team"].lower() in signal["text"].lower():
                        team = r["team"]
                        signal["team"] = team
                        break

            if not team:
                continue

            if team not in recent_signals:
                recent_signals[team] = []
            recent_signals[team].append(signal)

        # Check if any team has hit the threshold
        check_and_alert()

        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
