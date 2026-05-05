# Pinch-Hit Bot v2

Async event-driven rewrite of the pinch-hit alert bot. Receives reporter tweets via twitterapi.io webhooks, confirms via MLB GUMBO, posts two-tier Discord alerts.

## Quick Start

```bash
cd services/pinch-hit
pip install -e .
export TWITTERAPI_IO_KEY=...
export PINCH_HIT_WEBHOOK_URL=...
export OPS_WEBHOOK_URL=...
export ODDS_API_KEY=...
python -m pinch_hit.main
```

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TWITTERAPI_IO_KEY` | — | Yes | twitterapi.io bearer token |
| `PINCH_HIT_WEBHOOK_URL` | — | Yes | Discord webhook URL for pinch-hit alerts |
| `TWITTER_WEBHOOK_SECRET` | — | No | Shared secret expected as `/webhook?secret=...` for twitterapi.io delivery |
| `OPS_WEBHOOK_URL` | — | No | Discord webhook URL for ops notifications (degradation alerts, recovery) |
| `ODDS_API_KEY` | — | No | The Odds API key; odds PATCH skipped if not set |
| `DB_PATH` | `/data/pinch-hit.db` | No | Path to SQLite database file (Railway persistent volume mounts at `/data`) |
| `GAME_HOURS_START` | `12` | No | UTC hour to start monitoring (noon UTC = 8 AM ET) |
| `GAME_HOURS_END` | `6` | No | UTC hour to stop monitoring; wraps midnight when less than START |
| `LATE_INNING_THRESHOLD` | `6` | No | Minimum inning to spawn a GUMBO subscriber for a game |
| `TIMEOUT_MINUTES` | `5` | No | Minutes before a pending alert times out to grey |

Railway injects `PORT` automatically — the `/health` endpoint binds to it (fallback: 8080).
Configure twitterapi.io to POST matches to `/webhook`, optionally with `?secret=<TWITTER_WEBHOOK_SECRET>`.

## Shadow Validation

Run v2 alongside v1 in a separate Railway service with a shadow Discord channel:

1. Deploy v2 to Railway with `PINCH_HIT_WEBHOOK_URL` pointing to `#pinch-hits-v2`.
2. Leave v1 running unchanged, posting to `#pinch-hits`.
3. During a live game: compare alert timing and content between channels.
4. Check `evaluation_log` via `sqlite3 /data/pinch-hit.db "SELECT * FROM evaluation_log ORDER BY timestamp DESC LIMIT 20;"`.
5. Verify timeout watcher fires for stale alerts: insert a synthetic row with `posted_at` in the past and confirm the grey PATCH appears in `#pinch-hits-v2`.

## Cutover

When v2 shadow validation passes:

1. Update v2's `PINCH_HIT_WEBHOOK_URL` to point to the production `#pinch-hits` channel.
2. Stop v1 (`pinch_hit_bot.py`).
3. Archive the v1 process in Railway (or delete the service).

## Health Check

`GET /health` (port from `PORT` env var, fallback 8080):
- `200 OK` — system healthy or outside game hours
- `503 Service Unavailable` — Twitter webhook gap > 300s during game hours
