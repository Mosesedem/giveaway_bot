# Giveaway Bot — Deployment Guide

Written for someone who knows Go/Node but not Python. Where Python does
something unfamiliar, I'll call it out and give you the closest Go/Node
equivalent.

## What you're deploying

One process, two jobs:
- **Web dashboard** (FastAPI — think Express/Gin, but with built-in request
  validation and auto-generated API docs at `/docs`)
- **Background bot loop** (APScheduler — think a Go `time.Ticker` goroutine,
  or `node-cron`) running inside the *same* process, on a timer.

Both talk to the same Postgres database via SQLAlchemy (think GORM or
Prisma — an ORM that generates SQL from Python classes).

```
giveaway_bot/
├── app/
│   ├── main.py          ← FastAPI app + dashboard routes (like your routes/ dir)
│   ├── models.py         ← DB schema as Python classes (like GORM models)
│   ├── db.py              ← DB connection setup
│   ├── state_store.py     ← cursor/dedup tracking, Postgres-backed
│   ├── x_client.py        ← wraps the X (Twitter) API
│   ├── x_exceptions.py    ← typed error classes
│   ├── bot_logic.py        ← the actual bot behavior (command parsing, winner picking)
│   ├── scheduler.py        ← runs bot_logic on a timer
│   └── templates/          ← server-rendered HTML (Jinja2 — think Go's html/template)
├── requirements.txt        ← like package.json / go.mod
├── render.yaml              ← Render's infra-as-code config
└── .env.example
```

---

## Part 1 — Run it locally (5 minutes, no Postgres needed yet)

You don't need Python installed system-wide knowledge beyond this:

```bash
cd giveaway_bot

# Create an isolated dependency environment (like a local node_modules,
# but Python calls it a "virtual environment" because it isolates the
# Python interpreter + packages from your system Python)
python3 -m venv venv
source venv/bin/activate          # on Windows: venv\Scripts\activate

# Install dependencies (like npm install / go mod download)
pip install -r requirements.txt

# Copy the env template and fill in your real X API credentials
cp .env.example .env
# edit .env with your actual X_BEARER_TOKEN, X_API_KEY, etc.
```

Leave `DATABASE_URL` blank in `.env` for now — the app falls back to a
local SQLite file (`giveaway_bot_dev.db`) automatically. This lets you
run the whole thing with zero infra before touching Postgres.

Start it:

```bash
./run.sh
# or, with the venv active: python -m uvicorn app.main:app --reload
```

Use `python -m uvicorn` (or `./run.sh`) rather than bare `uvicorn` — if you have
uvicorn installed globally, the bare command can pick up your system Python and
fail with missing packages even though the venv has them.

`uvicorn` is the ASGI server running FastAPI — equivalent to running
`node server.js` or `go run main.go`. `--reload` is `nodemon`/`air`-style
hot reload, dev-only.

Open `http://localhost:8000` — you should see the dashboard. Also check
`http://localhost:8000/docs` — FastAPI auto-generates an interactive API
explorer from your route definitions, similar to Swagger but free.

**To test without hitting the live X API** (useful while you're just
poking at the dashboard), set `ENABLE_SCHEDULER=false` in `.env`. The
bot loop won't run, but you can still create giveaways manually from the
dashboard and exercise the winner-picking flow against entries you add
by hand.

---

## Part 2 — Deploy to Render

Render is the right call to start: it has a managed Postgres add-on,
deploys from a git push, and the `render.yaml` file in this project
already describes the whole setup — you don't have to click through
their UI to configure anything.

### Step 1 — Push to GitHub

Render deploys from a git repo, not a zip upload.

```bash
cd giveaway_bot
git init
git add .
git commit -m "Initial commit"
gh repo create giveaway-bot --private --source=. --push
# or: create a repo on github.com, then
#   git remote add origin <your-repo-url>
#   git push -u origin main
```

`.gitignore` is already set up to exclude `.env` and the dev SQLite file
— your X credentials won't end up in the repo.

### Step 2 — Create the Render Blueprint

1. Go to [render.com](https://render.com) → **New** → **Blueprint**
2. Connect your GitHub account, select the `giveaway-bot` repo
3. Render reads `render.yaml` automatically and shows you a preview:
   one **web service** + one **Postgres database**
4. Click **Apply**

This single step provisions Postgres and wires `DATABASE_URL` into your
web service automatically — you never type a connection string by hand.

### Step 3 — Set your secrets

`render.yaml` marks the X credentials as `sync: false`, meaning Render
won't store them in the blueprint (so they don't end up in your git
history). You set them once in the dashboard:

1. Render dashboard → your **giveaway-bot** web service → **Environment**
2. Add:
   - `X_BEARER_TOKEN`
   - `X_API_KEY`
   - `X_API_SECRET`
   - `X_ACCESS_TOKEN`
   - `X_ACCESS_TOKEN_SECRET`
3. Save — this triggers a redeploy automatically

### Step 4 — Verify

Render gives you a URL like `https://giveaway-bot-xxxx.onrender.com`.
Visit it — you should see the same dashboard you ran locally, now
backed by real Postgres. Check `/health` returns `{"status": "ok"}` —
that's what Render's own health checks hit to know your service is alive.

**Cold starts:** Render's free/starter tier spins down web services
after inactivity, and the *first* request after that can take 30-60s
to wake back up. This also means your background bot loop **stops
running** when the service sleeps. For a giveaway bot that needs to
poll X regularly, you'll likely want to upgrade off the free tier once
you're past testing, or use Render's cron-job product for the bot loop
specifically instead of relying on it living inside a web service.

---

## Part 3 — Operating Postgres day to day

You know SQL, so the main adjustment is tooling, not concepts.

**Connect directly** (for debugging, ad-hoc queries):
```bash
# Render dashboard → your database → "Connect" tab gives you a psql command, e.g.:
psql postgresql://giveaway_bot:xxxx@xxxx.render.com/giveaway_bot
```

**Schema changes:** right now `init_db()` (called on every app startup,
in `app/main.py`) just does `CREATE TABLE IF NOT EXISTS` — fine for
getting started, but it **won't alter existing tables** if you change a
model later (add a column, etc.). Once you're making schema changes
against real data, switch to **Alembic** (SQLAlchemy's migration tool —
the equivalent of `golang-migrate` or Prisma Migrate):

```bash
pip install alembic
alembic init migrations
# then: alembic revision --autogenerate -m "add column X"
#       alembic upgrade head
```

I didn't wire this up by default because it adds ceremony you don't
need on day one — but flag it to yourself once the schema stabilizes
and you have real giveaway data you can't afford to wipe.

**Backups:** Render's Postgres add-on takes automatic daily backups on
paid plans. Given this bot touches winner selection (i.e., real money
decisions), don't run on the free Postgres tier in production — verify
backups are enabled before your first real giveaway.

---

## Part 4 — Moving to your DigitalOcean box or EC2 later

Nothing in the code is Render-specific — `render.yaml` is the *only*
Render-specific file, and it's just config, not application logic. The
move is infra work, not a rewrite:

1. **Postgres**: either run Postgres on the same box (`apt install
   postgresql`) or keep using Render's managed Postgres and just point
   your new app server at it via `DATABASE_URL` — fully portable, it's
   a standard connection string.
2. **Process management**: Render handles "keep my process alive,
   restart on crash" for you. On your own box, use `systemd` (most
   direct Linux equivalent — write a `.service` file) or Docker with a
   restart policy. Since you know Go/Node tooling, `systemd` will feel
   familiar — it's the same job as a `pm2` ecosystem file.
3. **Reverse proxy**: put nginx or Caddy in front of uvicorn for TLS —
   Render does this for you automatically; on a VPS you do it yourself.
4. **Separate the scheduler from the web process** once you outgrow a
   single box: right now the bot loop runs inside the FastAPI process.
   At any real scale you'll want it as its own systemd unit/Docker
   service so a dashboard restart doesn't interrupt entry collection.
   `app/scheduler.py` is already structured so this split requires no
   code changes — just a second entrypoint that imports `bot_logic`
   directly instead of going through FastAPI.

A minimal `systemd` unit for reference, once you're there:
```ini
[Unit]
Description=Giveaway Bot
After=network.target postgresql.service

[Service]
WorkingDirectory=/opt/giveaway-bot
EnvironmentFile=/opt/giveaway-bot/.env
ExecStart=/opt/giveaway-bot/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## What's actually in the dashboard right now

- **Overview** (`/`) — active giveaway count, total entries, winners
  still needing a DM, last bot cycle time
- **Giveaways** (`/giveaways`) — list of all campaigns
- **New giveaway** (`/giveaways/new`) — manual creation (the bot also
  auto-creates these from X mentions containing "giveaway"/"start"/"begin")
- **Giveaway detail** — entries table, winner picking (random, excludes
  previously-selected users), per-winner "Send DM" button with retry-safe
  dedup (clicking it twice never double-sends)
- **Bot activity** (`/logs`) — cursor state per stream and recently
  processed tweets, for debugging what the bot has and hasn't seen

## What's deliberately not built yet

- **Auth** — the dashboard has zero login protection right now. Anyone
  with the URL can pick winners and send DMs. Fix this before you put
  real prize money behind it — easiest path is Render's "basic auth" via
  a small middleware, or stick the whole service behind a VPN/Cloudflare
  Access if you control the network.
- **Entry validation rules** — `is_valid`/`invalid_reason` columns exist
  on the `Entry` model but nothing populates them yet (e.g. "must follow
  the host," "one entry per account"). That's giveaway-specific business
  logic only you know — `bot_logic.collect_entries()` is where you'd add it.
- **Bulk DM rate limiting** — flagged in the original code review:
  notifying many winners at once will currently block on X's rate limit
  rather than queue. Fine for small giveaways, worth revisiting before
  a campaign with dozens of winners.
