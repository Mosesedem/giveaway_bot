# Giveaway Bot — Deployment Guide

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│  Web (API)  │     │    Worker    │     │  Postgres   │
│  Dashboard  │     │  Bot loop    │     │             │
│  Webhooks   │     │  DM queue    │     │             │
└──────┬──────┘     └──────┬───────┘     └──────┬──────┘
       │                   │                    │
       └───────────────────┴────────────────────┘
```

- **Web**: FastAPI + dashboard + webhooks. `ENABLE_SCHEDULER=false` on Render web tier.
- **Worker**: `python -m app.worker` — polls X, auto-picks winners, drains DM queue.
- **Cron wake**: `scripts/cron_wake.py` every 10 min (optional cold-start mitigation).

---

## Docker (recommended for local/staging)

```bash
cp .env.example .env
# Edit .env with X credentials (mocks OK for SAFEHAVEN/PAYSTACK locally)

docker compose up --build
```

Services: `db` → `migrate` → `web` + `worker`.

Production image only:

```bash
docker build -t giveaway-bot .
docker run -p 8000:8000 --env-file .env giveaway-bot
```

Worker container:

```bash
docker run --env-file .env giveaway-bot python -m app.worker
```

---

## Render (production)

`render.yaml` provisions:

| Service | Role |
|---------|------|
| `giveaway-bot` (web) | Dashboard, webhooks, health |
| `giveaway-bot-worker` | 24/7 bot loop |
| `giveaway-bot-wake` (cron) | Keep-alive ping |
| `giveaway-bot-db` | Postgres |

### Deploy steps

1. Push repo to GitHub.
2. Render → **New Blueprint** → select repo → Apply.
3. Set **sync: false** secrets on **web AND worker**:
   - All `X_*` credentials
   - `PUBLIC_BASE_URL` (web URL, `https://…`)
   - `SAFEHAVEN_CLIENT_ID`, `SAFEHAVEN_PRIVATE_KEY` (or `SAFEHAVEN_CLIENT_ASSERTION`)
   - `SAFEHAVEN_DEBIT_ACCOUNT`, `SAFEHAVEN_SETTLEMENT_ACCOUNT_NUMBER`
   - `SAFEHAVEN_WEBHOOK_SECRET`
   - `PAYSTACK_SECRET_KEY`
   - `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `CRON_WAKE_SECRET`

4. Run migrations (included in build): `alembic upgrade head`

### Production defaults (render.yaml)

- `FINTECH_MODE=true`
- `AUTO_PICK_WINNERS=true`
- `ENABLE_BOT_REPLIES=true`
- `REQUIRE_WEBHOOK_SECRET=true`

---

## CI

GitHub Actions (`.github/workflows/ci.yml`) on every push/PR:

- `pytest tests/`
- `docker build` + health smoke test

---

## Webhook URLs (register with providers)

| Provider | URL |
|----------|-----|
| SafeHaven | `{PUBLIC_BASE_URL}/webhooks/safehaven/virtual-account` |
| Paystack | `{PUBLIC_BASE_URL}/webhooks/paystack` |

SafeHaven events: `virtualAccount.transfer`, `account.credit`, `account.debit`.  
Paystack events: `charge.success`, `transfer.success`, `transfer.failed`, `transfer.reversed`.

---

## Health checks

```bash
curl https://your-app.onrender.com/health
curl https://your-app.onrender.com/health/x
python scripts/sandbox_smoke.py
```

---

## Local venv (without Docker)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m alembic upgrade head
./run.sh
```

Use `ENABLE_SCHEDULER=false` for dashboard-only debugging.