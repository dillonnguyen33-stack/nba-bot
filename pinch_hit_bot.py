"""
MLB Pinch Hit Alert Bot
Monitors beat reporters + general Twitter for pinch hit keywords
Posts alerts to Discord with player lines
Requires 3+ sources within 3 minute window to fire alert
Fixes:
  1) No duplicate alerts — tweet IDs tracked in DB style set + alert key per player/team/window
  2) Clean summary — "X will be pinch hit for Y"
  3) Pre-event only — rejects past tense / result tweets
"""

import os
import re
import time
import requests
from datetime import datetime, timezone

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY")
POLL_INTERVAL        = 60
ALERT_WINDOW         = 180
MIN_SOURCES          = 3

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

# ── KEYWORDS — pre-event only ─────────────────────────────────────────────────
PINCH_HIT_KEYWORDS = [
    "pinch hit", "pinch hitting", "pinch hitter", "pinch-hit",
    "on deck for", "batting for", "will bat for",
    "coming out for", "being lifted for", "ph for",
]

# ── RESULT/RECAP PHRASES — reject these (past tense, outcome language) ────────
REJECT_PHRASES = [
    "home run", "homered", "hit a", "singled", "doubled", "tripled",
    "drove in", "rbi", "scores", "scored", "flies out", "grounds out",
    "struck out", "strikeout", "walks", "walked", "career",
    "first career", "solo shot", "connects", "connected",
    "reaches", "reached", "pops out", "lines out",
    "pinch-hit home run", "pinch hit home run",
    "pinch-hit single", "pinch hit single",
    "pinch-hit rbi", "pinch hit rbi",
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

# ── STATE ─────────────────────────────────────────────────────────────────────
recent_signals = {}
seen_tweet_ids = set()   # permanent — never forget a tweet ID
posted_alerts  = set()   # permanent — never post same alert twice

TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

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
def contains_keyword(text):
    tl = text.lower()
    return any(kw in tl for kw in PINCH_HIT_KEYWORDS)

def is_pre_event(text):
    """
    Return True only if the tweet is about something ABOUT TO happen.
    Rejects past tense results, recaps, and outcome language.
    """
    tl = text.lower()
    for phrase in REJECT_PHRASES:
        if phrase in tl:
            return False
    return True

def extract_pinch_hitter_and_replaced(text):
    """
    Try to extract who is pinch hitting AND who they are replacing.
    Returns (pinch_hitter, replaced_player) — either can be None.
    """
    # Pattern: "X will pinch hit for Y" or "X pinch hitting for Y"
    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?batting\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, text)
        if m:
            return m.group(1), m.group(2)

    # Pattern: just pinch hitter
    patterns_hitter = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'(?:ph|pinch[- ]hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_hitter:
        m = re.search(p, text)
        if m:
            return m.group(1), None

    # Pattern: just who is coming out
    patterns_out = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?coming\s+out',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?(?:being\s+)?lifted',
    ]
    for p in patterns_out:
        m = re.search(p, text)
        if m:
            return None, m.group(1)

    return None, None

def build_summary(signals):
    """Build a clean human-readable summary from signals."""
    pinch_hitters = []
    replaced      = []

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
    else:
        return "Pinch hit situation confirmed"

def find_pinch_hitter(signals):
    """Return the most commonly mentioned pinch hitter."""
    players = [s["pinch_hitter"] for s in signals if s.get("pinch_hitter")]
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
                            results[key] = {"book": bname, "market": label, "line": pt, "under": pr}
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
def post_alert(team, signals, lines_data):
    if not DISCORD_WEBHOOK_URL:
        return

    rc      = sum(1 for s in signals if s["is_reporter"])
    gc      = len(signals) - rc
    summary = build_summary(signals)

    # Source tweets (max 3, deduplicated by handle)
    seen_handles = set()
    src_lines    = []
    for s in signals:
        if s["handle"] in seen_handles:
            continue
        seen_handles.add(s["handle"])
        label = "🎙️ Reporter" if s["is_reporter"] else "🌐 Twitter"
        src_lines.append(f"{label} **@{s['handle']}:** _{s['text'][:100]}_\n🔗 [Tweet]({s['url']})")
        if len(src_lines) >= 3:
            break

    embed = {"embeds": [{"title": f"⚾🚨 PINCH HIT ALERT — {team}",
        "description": (
            f"**{len(signals)} sources confirmed** ({rc} reporters + {gc} general)\n\n"
            f"📋 **{summary}**\n\n"
            + "\n\n".join(src_lines) +
            f"\n\n{format_lines(lines_data)}\n\n"
            f"💰 **BET THE UNDER ON ALL LINES NOW**"
        ),
        "color": 0x00FF00,
        "footer": {"text": f"Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}}]}
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=embed, timeout=10).raise_for_status()
        print(f"  ✅ Alert posted: {team} — {summary}")
    except Exception as e:
        print(f"[discord error] {e}")

# ── PROCESS ───────────────────────────────────────────────────────────────────
def process_tweets(tweets, users):
    now = datetime.now(timezone.utc).timestamp()
    new_signals = []
    for tweet in tweets:
        tid    = tweet.get("id") or tweet.get("tweet_id", "")
        text   = tweet.get("text", "")
        aid    = tweet.get("author_id", "")
        handle = users.get(aid, tweet.get("handle", "unknown")).lower()

        # ── FIX 1: strict dedup by tweet ID ──────────────────────────────────
        if tid in seen_tweet_ids:
            continue
        seen_tweet_ids.add(tid)

        # ── FIX 3: keyword check ──────────────────────────────────────────────
        if not contains_keyword(text):
            continue

        # ── FIX 3: reject past tense / result tweets ──────────────────────────
        if not is_pre_event(text):
            print(f"  🚫 Rejected (past tense/result): @{handle}: {text[:80]}")
            continue

        is_reporter              = handle in REPORTER_HANDLES
        reporter                 = REPORTER_BY_HANDLE.get(handle)
        team                     = reporter["team"] if reporter else None
        pinch_hitter, replaced   = extract_pinch_hitter_and_replaced(text)
        url                      = f"https://twitter.com/{handle}/status/{tid}"

        new_signals.append({
            "handle": handle, "text": text, "url": url,
            "pinch_hitter": pinch_hitter, "replaced": replaced,
            "team": team, "is_reporter": is_reporter, "timestamp": now,
        })
        print(f"  📡 @{handle} ({'rep' if is_reporter else 'gen'}) "
              f"team={team} ph={pinch_hitter} out={replaced}")

    return new_signals

def check_and_alert():
    now = datetime.now(timezone.utc).timestamp()
    for team in list(recent_signals.keys()):
        recent_signals[team] = [s for s in recent_signals[team]
                                 if now - s["timestamp"] <= ALERT_WINDOW]
        active = recent_signals[team]
        if len(active) < MIN_SOURCES:
            continue

        pinch_hitter = find_pinch_hitter(active)
        time_bucket  = int(now / ALERT_WINDOW)

        # ── FIX 1: unique key per team + player + time window ─────────────────
        alert_key = f"{team}_{pinch_hitter}_{time_bucket}"
        if alert_key in posted_alerts:
            continue
        posted_alerts.add(alert_key)

        lines_data = get_player_lines(pinch_hitter) if pinch_hitter else {}
        post_alert(team, active, lines_data)

def add_signals(new_signals):
    for s in new_signals:
        team = s["team"]
        if not team:
            tl = s["text"].lower()
            for r in REPORTERS:
                if r["team"].lower() in tl:
                    team = r["team"]
                    s["team"] = team
                    break
        if not team:
            continue
        recent_signals.setdefault(team, []).append(s)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print(f"⚾ MLB Pinch Hit Bot started! {len(REPORTERS)} reporters, "
          f"threshold={MIN_SOURCES}, window={ALERT_WINDOW}s\n")

    if not TWITTER_BEARER_TOKEN:
        print("[error] TWITTER_BEARER_TOKEN not set!")
        return

    print("Looking up reporter user IDs...")
    handles  = [r["handle"] for r in REPORTERS]
    user_ids = {}
    for i in range(0, len(handles), 100):
        ids = get_user_ids_batch(handles[i:i+100])
        user_ids.update(ids)
    print(f"Found {len(user_ids)} user IDs\n")

    cycle = 0

    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle {cycle}")
        all_new = []

        # General keyword searches (short queries)
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
            sigs = process_tweets(tweets, {uid: reporter["handle"]})
            all_new.extend(sigs)
            time.sleep(1)

        add_signals(all_new)
        check_and_alert()
        cycle += 1
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    run()
