# Giveaway Bot — API Use & Live Testing Guide

Step-by-step guide for using the bot with the real X (Twitter) API: credentials,
verification, a safe dry run, then a full live giveaway test.

For deployment and infrastructure, see [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md).

---

## What the bot does on X

| Phase | What happens | X API used |
|-------|----------------|------------|
| **Listen** | Bot checks `@YourBot giveaway …` mentions | `GET /2/users/:id/mentions` |
| **Collect** | Bot pulls replies in the giveaway thread | `GET /2/tweets/search/recent` (`conversation_id:…`) |
| **Pick** | Random winner selection (dashboard) | No X call — runs in your DB |
| **Validate** | Check follow/age/followers/keyword on each reply | `GET friendships/show` + `GET /2/users/:id` |
| **Notify** | DM each winner (immediate or queued) | `POST /2/dm_conversations/.../messages` |

The background loop runs every `BOT_CYCLE_SECONDS` (default 90s) when
`ENABLE_SCHEDULER=true`. Each cycle also drains up to `DM_BATCH_SIZE`
queued DMs (default 3), spaced `DM_INTERVAL_SECONDS` apart.

---

## Part 1 — X Developer Portal setup

### 1. Create a project and app

1. Go to [developer.x.com](https://developer.x.com) and sign in.
2. Create a **Project** and an **App** under it.
3. Note your app's **API Key**, **API Secret**, **Bearer Token**, **Access Token**, and **Access Token Secret**.

The bot account's access token must belong to the **same account the bot tweets/DMs from** — not a separate personal account.

### 2. Confirm API access level

This bot needs endpoints that require at least **Basic** access on the X API v2:

- Read mentions for the bot user
- Search recent tweets by `conversation_id` (last **7 days** of replies)
- Post tweets/replies (future confirmation messages)
- Send direct messages

If any call returns `403` or "product not enabled", upgrade your developer access in the portal before going live.

### 3. Enable OAuth 1.0a user context

The bot uses **user-context** auth (your access token + secret), not app-only bearer auth alone. In the developer portal:

1. Open your app → **User authentication settings** → **Set up**.
2. Enable **Read and write** and **Direct message** permissions.
3. Regenerate the **Access Token and Secret** for the bot account after changing permissions.

### 4. Bot account checklist

Use a dedicated X account for the bot (recommended):

- [ ] Profile photo and bio explain it's an official giveaway bot
- [ ] DMs are open (or winners must follow you — X policy varies; test with a friend account)
- [ ] The account can post and send DMs (not restricted/suspended)

---

## Part 2 — Local credentials

### 1. Copy and fill `.env`

```bash
cd giveaway_bot
cp .env.example .env
```

Fill in all five X variables:

```env
X_BEARER_TOKEN=...
X_API_KEY=...
X_API_SECRET=...
X_ACCESS_TOKEN=...
X_ACCESS_TOKEN_SECRET=...
```

Leave `DATABASE_URL` blank for local SQLite, or delete the line entirely.

### 2. Optional safety settings

```env
# Dashboard-only while wiring up the UI (no background X polling)
ENABLE_SCHEDULER=false

# Faster cycles during testing (minimum ~30s recommended)
BOT_CYCLE_SECONDS=60

# Only these X user IDs can auto-start giveaways via mention (numeric IDs)
TRUSTED_HOST_USER_IDS=1234567890

# Protect the dashboard before going public
DASHBOARD_USER=admin
DASHBOARD_PASSWORD=your-strong-password

# Entry validation (enable for real giveaways)
REQUIRE_FOLLOW_HOST=true
MIN_ACCOUNT_AGE_DAYS=7
MIN_FOLLOWERS=10
REQUIRE_ENTRY_KEYWORD=#giveaway

# DM queue tuning (for many winners)
DM_BATCH_SIZE=3
DM_INTERVAL_SECONDS=15

# Render keep-alive (set same secret on web service + cron job)
CRON_WAKE_SECRET=your-long-random-string
```

To find a host's numeric user ID: use [tweeterid.com](https://tweeterid.com/) or the X API `GET /2/users/by/username/:username`.

### 3. Install and start

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
./run.sh
```

Open `http://localhost:8000`.

---

## Part 3 — Verify X API connectivity

Run these **before** your first real giveaway.

### Option A — CLI script (no server needed)

```bash
source venv/bin/activate
python scripts/test_x_auth.py
```

Expected output:

```
OK: authenticated as @YourBotName (user_id=1234567890)
```

### Option B — HTTP health check (server running)

```bash
curl -s http://localhost:8000/health/x | python3 -m json.tool
```

Expected:

```json
{
  "ok": true,
  "username": "YourBotName",
  "user_id": "1234567890"
}
```

### Option C — Process health (no X call)

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

Check `x_configured: true` and `last_cycle_error: null` after the scheduler has run at least once.

### If verification fails

| Symptom | Likely cause | Fix |
|---------|----------------|-----|
| `missing credentials` | Empty `.env` keys | Fill all five `X_*` vars; restart the app |
| `401 Unauthorized` | Bad token or regenerated keys | Regenerate tokens in developer portal; update `.env` |
| `403 Forbidden` on mentions/search | API tier too low | Upgrade X API access |
| `403` on DM | DM permission not enabled | Re-enable DM scope; regenerate access token |
| App starts but cycles skip | Scheduler on, creds missing | Check overview banner or `/health` |

---

## Part 4 — Dry run (dashboard only, no live X polling)

Use this to learn the UI without spending API quota.

1. Set `ENABLE_SCHEDULER=false` in `.env` and restart.
2. Open `http://localhost:8000/giveaways/new`.
3. Create a test giveaway with any title. Conversation ID can be blank for this step.
4. You won't have real entries yet — the dry run proves the dashboard loads and forms work.

To test winner picking without X, you'd need to insert test rows into the DB manually (advanced). For a realistic test, continue to Part 5.

---

## Part 5 — Live end-to-end test (recommended before real money)

Do this with a **throwaway prize** (e.g. "₦100 test") and accounts you control.

### Step 1 — Enable the scheduler

```env
ENABLE_SCHEDULER=true
BOT_CYCLE_SECONDS=60
```

Restart `./run.sh`. Confirm the overview shows **Last bot cycle** updating and no error banner.

### Step 2 — Auto-start via mention (optional)

From a **trusted host account** (or any account if `TRUSTED_HOST_USER_IDS` is unset), post:

```
@YourBotName giveaway test — ₦100 Friday test
```

Wait one bot cycle (~60–90s), then check:

- **Overview** → active giveaway count increases
- **Giveaways** → new row with title from the tweet
- **Bot activity** (`/logs`) → processed tweet with context `giveaway_created:…`

If nothing appears:

1. Confirm the tweet is a **mention** of the bot (not just a quote)
2. Tweet must contain `giveaway`, `start`, or `begin`
3. Check `/logs` for `ignored:untrusted_host` if `TRUSTED_HOST_USER_IDS` is set
4. Click **Collect entries** won't work until `conversation_id` is set — auto-created giveaways set this from the mention tweet

### Step 3 — Manual giveaway (alternative path)

If you prefer full control:

1. Post a host tweet: *"Reply to enter — ₦100 test giveaway"*
2. Copy the tweet ID from the URL: `https://x.com/user/status/**1234567890123456789**`
3. Dashboard → **New giveaway** → paste that ID as **Conversation ID**
4. Create the giveaway

### Step 4 — Collect entries

1. From 2–3 test accounts, **reply** to the host tweet (each account once — duplicate users are ignored)
2. Wait for a bot cycle, or on the giveaway detail page click **Collect entries now**
3. Refresh — entries table should list each reply (bot and host replies are excluded automatically)

**7-day window:** X's recent search only indexes ~7 days of tweets. Run collection at least once every few days during long campaigns.

### Step 5 — Pick winners

1. On the giveaway detail page, click **Pick winners**
2. Confirm winner row(s) appear with status `selected`
3. Giveaway status moves to `winners_selected`

### Step 6 — Notify winners (real DM)

**Option A — immediate:** click **Send now** on a winner row.

**Option B — queued (recommended for 3+ winners):** click **Queue all DMs**.
The scheduler sends up to `DM_BATCH_SIZE` per cycle, waiting `DM_INTERVAL_SECONDS`
between each. Overview shows **DM queue** depth.

1. Use message template:  
   `"Congrats! You won the test giveaway. Reply with your bank details."`
2. Click **Queue all DMs** (or **Queue DM** per winner)
3. Wait 1–2 bot cycles; check winner inbox — status should change to `notified`

**Dedup safety:** Queueing or sending twice does **not** double-send (dedup key per giveaway + user).

If DM fails (`dm_failed`):

- Winner may have DMs closed or not follow the bot
- Check X developer portal DM permissions
- Retry from the dashboard — only the first successful send is deduped

### Step 7 — Inspect bot state

Visit `/logs`:

- **Cursors** — `mentions` and `thread:<conversation_id>` show how far the bot has read
- **Processed tweets** — audit trail (`entry:…`, `ignored:duplicate_user`, etc.)

---

## Part 6 — Entry validation

Enable rules in `.env` before collecting entries on a real campaign.

| Variable | Effect |
|----------|--------|
| `REQUIRE_FOLLOW_HOST=true` | Entrant must follow the host (`host_user_id` on giveaway) |
| `REQUIRE_FOLLOW_BOT=true` | Entrant must follow the bot account |
| `MIN_ACCOUNT_AGE_DAYS=7` | Account must be at least N days old |
| `MIN_FOLLOWERS=10` | Account must have at least N followers |
| `REQUIRE_ENTRY_KEYWORD=#giveaway` | Reply text must contain keyword (case-insensitive) |

Invalid entries are still stored with `is_valid=false` and `invalid_reason` set —
useful for auditing disputes. Only `is_valid=true` entries are eligible for winner picking.

After changing rules mid-campaign, click **Revalidate entries** on the giveaway page.

**Test validation:** have a test account that does *not* follow the host reply to
the thread — it should appear in entries as invalid with reason `must follow the host`.

---

## Part 7 — Production checklist

Before a real giveaway with real money:

- [ ] `python scripts/test_x_auth.py` passes
- [ ] `/health/x` returns `ok: true` on your deployed URL
- [ ] `DASHBOARD_USER` and `DASHBOARD_PASSWORD` set
- [ ] `TRUSTED_HOST_USER_IDS` set to your official host account(s)
- [ ] `REQUIRE_FOLLOW_HOST=true` (and other validation rules you need)
- [ ] `CRON_WAKE_SECRET` set on Render web service **and** wake cron picks it up
- [ ] `alembic upgrade head` succeeded on deploy (check Render build logs)
- [ ] `ENABLE_SCHEDULER=true` on starter plan or higher
- [ ] Postgres (not SQLite) for production data
- [ ] Manual backup plan if X API is down

---

## Part 8 — Schema migrations

```bash
source venv/bin/activate
alembic upgrade head
```

After changing `app/models.py`:

```bash
alembic revision --autogenerate -m "add column foo"
alembic upgrade head
```

If you have an existing SQLite/Postgres DB created before Alembic was added:

```bash
alembic stamp head   # mark as migrated without re-creating tables
```

---

## Part 9 — Render keep-alive (cold start mitigation)

`render.yaml` includes a cron job `giveaway-bot-wake` that runs every 10 minutes
and calls `GET /internal/wake` on your web service. This keeps the process alive
on starter tier and re-ensures the scheduler is running.

**Setup on Render:**

1. Deploy the blueprint (includes web + cron + Postgres)
2. Set `CRON_WAKE_SECRET` on the **giveaway-bot** web service (Environment tab)
3. Redeploy — the cron job inherits the same secret via `fromService`

**Verify locally:**

```bash
CRON_WAKE_SECRET=testsecret ./run.sh
# in another terminal:
curl -H "Authorization: Bearer testsecret" http://localhost:8000/internal/wake
```

This is not a substitute for an always-on paid plan at scale, but prevents
most cold-start gaps during active giveaway windows.

---

## Part 10 — Operating during a live giveaway

### Typical timeline

```
Host posts giveaway tweet
        ↓
Bot detects mention OR you create giveaway manually with conversation ID
        ↓
Entrants reply to the thread (days/hours)
        ↓
Bot collects entries every BOT_CYCLE_SECONDS (or manual Collect)
        ↓
Host closes entries → dashboard → Pick winners
        ↓
Send DMs one by one (or in batches — watch rate limits for large winner counts)
        ↓
Mark paid / add notes manually (payout tracking is dashboard-only for now)
```

### Manual overrides

| Action | When to use |
|--------|-------------|
| **Collect entries now** | Force a sync without waiting for the scheduler |
| **Pick winners** | Host says "entries closed" — run immediately |
| **Send DM** | Per-winner notification with custom message |
| **New giveaway** | Host coordinated offline; you have the thread ID |

### Rate limits

X enforces per-endpoint rate limits. The client retries transient errors automatically.
For many winners, use **Queue all DMs** — the scheduler respects `DM_BATCH_SIZE` and
`DM_INTERVAL_SECONDS`. On rate limit, items stay `pending` and retry next cycle.

---

## Part 11 — Troubleshooting reference

### "No conversation ID on this giveaway"

The giveaway has no thread to scan. Edit by creating a new giveaway with the correct host tweet ID, or ensure auto-start picked up the mention (which sets `conversation_id` from the command tweet).

### Entries not appearing

1. Replies must be in the **same conversation** (direct replies to the host tweet)
2. `conversation_id` must be the **root tweet ID**
3. Search index can lag a few minutes
4. Check `/logs` for cursor advancement on `thread:<id>`

### Same user entered twice

Only the **first reply per user** counts. Later replies are marked `ignored:duplicate_user` in `/logs`.

### Scheduler runs but nothing changes

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

Look at `last_cycle_error`. Common values:

- `X API credentials not configured` — fill `.env`
- `403` / `429` — permissions or rate limit

### jinja2 / wrong Python on startup

Always start with:

```bash
./run.sh
# or: python -m uvicorn app.main:app --reload
```

Never rely on a globally installed `uvicorn` binary.

---

## Quick reference — URLs

| URL | Purpose |
|-----|---------|
| `/` | Overview and health banners |
| `/giveaways` | All campaigns |
| `/giveaways/new` | Manual creation |
| `/giveaways/{id}` | Entries, pick winners, send DMs |
| `/logs` | Cursors and processed-tweet audit trail |
| `/health` | Uptime check (Render) + cycle status + DM queue depth |
| `/health/x` | Live X API auth test |
| `/internal/wake` | Keep-alive ping (requires `CRON_WAKE_SECRET` if set) |
| `/docs` | Auto-generated API explorer |

---

## Quick reference — commands

```bash
# Start locally
./run.sh

# Test credentials
python scripts/test_x_auth.py

# Test live auth endpoint
curl http://localhost:8000/health/x

# Dashboard-only mode
# .env: ENABLE_SCHEDULER=false
```

Once Parts 3 and 5 pass with test accounts, you're ready to run a real campaign.