"""
MLB Pinch Hit Alert Bot
Monitors beat reporters + general Twitter for pinch hit keywords
Posts alerts to Discord with player lines
Requires 3+ sources within 3 minute window to fire alert
Upgrades:
  - Game hours only (12pm - 1am ET) to save API credits
  - Lineup check via MLB API to verify replaced player was in starting lineup
  - Confidence score shown subtly in alert
  - Daily reset of seen_tweet_ids at midnight
  - @everyone ping on every alert
"""

import os
import re
import time
import requests
from datetime import datetime, timezone, timedelta
import pytz

# ── CONFIG ────────────────────────────────────────────────────────────────────
TWITTER_BEARER_TOKEN = os.environ.get("TWITTER_BEARER_TOKEN")
DISCORD_WEBHOOK_URL  = os.environ.get("PINCH_HIT_WEBHOOK_URL")
ODDS_API_KEY         = os.environ.get("ODDS_API_KEY")
POLL_INTERVAL        = 30
ALERT_WINDOW         = 180
MIN_SOURCES          = 3
ET_TZ                = pytz.timezone("America/New_York")
GAME_START_HOUR      = 12
GAME_END_HOUR        = 25

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

PINCH_HIT_KEYWORDS = [
    "pinch hit", "pinch hitting", "pinch hitter", "pinch-hit",
    "on deck for", "batting for", "will bat for",
    "coming out for", "being lifted for", "ph for",
]

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

PROP_BOOKS = {
    "draftkings":  "DraftKings",
    "fanduel":     "FanDuel",
    "fliff":       "Fliff",
    "hardrockbet": "Hard Rock",
    "bet365":      "Bet365",
}

MLB_PROP_MARKETS = ["batter_hits", "batter_total_bases", "batter_rbis", "batter_home_runs"]

MLB_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer":    "https://www.nba.com/",
    "Accept":     "application/json",
}

# ── STATE ─────────────────────────────────────────────────────────────────────
recent_signals  = {}
seen_tweet_ids  = set()
posted_alerts   = set()
last_reset_date = None

TWITTER_HEADERS = {"Authorization": f"Bearer {TWITTER_BEARER_TOKEN}"}

# ── GAME HOURS CHECK ──────────────────────────────────────────────────────────
def is_game_hours():
    now_et = datetime.now(ET_TZ)
    hour   = now_et.hour
    return hour >= 12 or hour == 0

# ── DAILY RESET ───────────────────────────────────────────────────────────────
def maybe_reset_daily():
    global seen_tweet_ids, last_reset_date, recent_signals
    today = datetime.now(ET_TZ).date()
    if last_reset_date is None:
        last_reset_date = today
        return
    if today > last_reset_date:
        print(f"[reset] New day — clearing seen tweet IDs and recent signals")
        seen_tweet_ids  = set()
        recent_signals  = {}
        last_reset_date = today

# ── MLB API — LINEUP CHECK ────────────────────────────────────────────────────
def get_live_game_ids():
    mlb_url = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&gameType=R&fields=dates,games,gamePk,status,abstractGameState"
    try:
        r = requests.get(mlb_url, timeout=10)
        r.raise_for_status()
        data  = r.json()
        games = []
        for date in data.get("dates", []):
            for game in date.get("games", []):
                state = game.get("status", {}).get("abstractGameState", "")
                if state == "Live":
                    games.append(game["gamePk"])
        return games
    except Exception as e:
        print(f"[mlb scoreboard error] {e}")
        return []

def get_starting_lineup(game_pk):
    url = f"https://statsapi.mlb.com/api/v1.1/game/{game_pk}/feed/live"
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data     = r.json()
        boxscore = data.get("liveData", {}).get("boxscore", {})
        starters = set()
        for side in ["home", "away"]:
            team   = boxscore.get("teams", {}).get(side, {})
            roster = team.get("players", {})
            for pid, pdata in roster.items():
                bo = pdata.get("battingOrder", "")
                if bo and str(bo).endswith("0"):
                    name = pdata.get("person", {}).get("fullName", "").lower()
                    if name:
                        starters.add(name)
        return starters
    except Exception as e:
        print(f"[lineup error game {game_pk}] {e}")
        return set()

def verify_in_lineup(player_name, team):
    if not player_name:
        return None
    game_ids   = get_live_game_ids()
    if not game_ids:
        return None
    name_lower = player_name.lower()
    for game_pk in game_ids[:6]:
        starters = get_starting_lineup(game_pk)
        for starter in starters:
            if name_lower.split()[-1] in starter:
                return True
    return False

# ── CONFIDENCE SCORE ──────────────────────────────────────────────────────────
def calculate_confidence(signals, lineup_verified):
    score = 0
    num   = len(signals)
    if num >= 5:   score += 5
    elif num >= 4: score += 4
    elif num >= 3: score += 3
    elif num >= 2: score += 2
    else:          score += 1

    reporters = sum(1 for s in signals if s["is_reporter"])
    if reporters >= 3:   score += 3
    elif reporters >= 2: score += 2
    elif reporters >= 1: score += 1

    if lineup_verified is True:   score += 2
    elif lineup_verified is None: score += 1

    return min(score, 10)

def confidence_emoji(score):
    if score >= 8:   return "🟢"
    elif score >= 6: return "🟡"
    else:            return "🔴"

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
    return any(kw in text.lower() for kw in PINCH_HIT_KEYWORDS)

def is_pre_event(text):
    tl = text.lower()
    return not any(phrase in tl for phrase in REJECT_PHRASES)

def extract_pinch_hitter_and_replaced(text):
    patterns_both = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit(?:ting)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?batting\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:is\s+)?ph(?:ing)?\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_both:
        m = re.search(p, text)
        if m:
            return m.group(1), m.group(2)

    patterns_hitter = [
        r'([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)\s+(?:will\s+)?pinch[- ]hit',
        r'(?:ph|pinch[- ]hit)\s+for\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)+)',
    ]
    for p in patterns_hitter:
        m = re.search(p, text)
        if m:
            return m.group(1), None

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
    else:
        return "Pinch hit situation confirmed"

def find_pinch_hitter(signals):
    players = [s["pinch_hitter"] for s in signals if s.get("pinch_hitter")]
    if not players:
        return None
    counts = {}
    for p in players:
        counts[p.lower()] = counts.get(p.lower(), 0) + 1
    return max(counts, key=counts.get).title()

def find_replaced(signals):
    players = [s["replaced"] for s in signals if s.get("replaced")]
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
def post_alert(team, signals, lines_data, confidence, lineup_verified):
    if not DISCORD_WEBHOOK_URL:
        return

    rc      = sum(1 for s in signals if s["is_reporter"])
    gc      = len(signals) - rc
    summary = build_summary(signals)
    conf_em = confidence_emoji(confidence)

    if lineup_verified is True:
        lineup_note = "✅ Starter confirmed in lineup"
    elif lineup_verified is False:
        lineup_note = "⚠️ Player not found in starting lineup — may be injury sub"
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
            f"**{len(signals)} sources confirmed** ({rc} reporters + {gc} general)\n"
            f"{conf_em} **Confidence: {confidence}/10** | {lineup_note}\n\n"
            f"📋 **{summary}**\n\n"
            + "\n\n".join(src_lines) +
            f"\n\n{format_lines(lines_data)}\n\n"
            f"💰 **BET THE UNDER ON ALL LINES NOW**"
        ),
        "color": 0x00FF00 if confidence >= 7 else 0xF1C40F,
        "footer": {"text": f"Pinch Hit Bot · {datetime.utcnow().strftime('%H:%M UTC')}"}}]}
    try:
        # ── @everyone ping on every alert ─────────────────────────────────────
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": "@everyone", "embeds": embed["embeds"]},
            timeout=10
        ).raise_for_status()
        print(f"  ✅ Alert: {team} — {summary} (conf={confidence})")
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

        if tid in seen_tweet_ids:
            continue
        seen_tweet_ids.add(tid)

        if not contains_keyword(text):
            continue

        if not is_pre_event(text):
            print(f"  🚫 Rejected: @{handle}: {text[:80]}")
            continue

        is_reporter            = handle in REPORTER_HANDLES
        reporter               = REPORTER_BY_HANDLE.get(handle)
        team                   = reporter["team"] if reporter else None
        pinch_hitter, replaced = extract_pinch_hitter_and_replaced(text)
        url                    = f"https://twitter.com/{handle}/status/{tid}"

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

        # ── RULE: must have at least 1 verified beat reporter ─────────────────
        reporter_count = sum(1 for s in active if s["is_reporter"])
        if reporter_count < 1:
            continue

        # ── RULE: must have at least 3 total sources ──────────────────────────
        if len(active) < MIN_SOURCES:
            continue

        pinch_hitter = find_pinch_hitter(active)
        replaced     = find_replaced(active)
        time_bucket  = int(now / ALERT_WINDOW)
        alert_key    = f"{team}_{pinch_hitter}_{time_bucket}"

        if alert_key in posted_alerts:
            continue
        posted_alerts.add(alert_key)

        lineup_verified = verify_in_lineup(replaced, team) if replaced else None
        confidence      = calculate_confidence(active, lineup_verified)
        lines_data      = get_player_lines(pinch_hitter) if pinch_hitter else {}
        post_alert(team, active, lines_data, confidence, lineup_verified)

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
        maybe_reset_daily()

        now_et = datetime.now(ET_TZ)
        hour   = now_et.hour

        if not (hour >= 12 or hour == 0):
            print(f"[{now_et.strftime('%H:%M ET')}] Outside game hours — sleeping 10 min")
            time.sleep(600)
            continue

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Cycle {cycle}")
        all_new = []

        for kw in ["pinch hit", "pinch-hit"]:
            query = f'"{kw}" baseball -is:retweet lang:en'
            data  = search_tweets(query, max_results=15)
            tweets = data.get("data", [])
            users  = {u["id"]: u["username"] for u in
                      data.get("includes", {}).get("users", [])}
            all_new.extend(process_tweets(tweets, users))
            time.sleep(2)

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
