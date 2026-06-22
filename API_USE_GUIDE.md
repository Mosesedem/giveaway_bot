# Giveaway Bot — API Use & Live Testing Guide

Fintech giveaway flow (SafeHaven + Paystack) with X integration. For infrastructure see [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md). For money flow see [FINTECH_FLOW.md](FINTECH_FLOW.md).

---

## What the bot does

| Phase | What happens | X API |
|-------|----------------|-------|
| **Host command** | `@Bot giveaway ₦5k / winners: 3 / duration: 7d` | Mentions |
| **Intake** | Collects missing amount, winners, duration | Thread replies |
| **Funding** | Issues per-giveaway VA; host pays prize + fee | Thread reply with VA |
| **Activate** | Webhook confirms payment → giveaway LIVE | Receipt reply on thread |
| **Entries** | Thread replies collected until `closes_at` | Thread search |
| **Auto-close** | Closes entries, **random winner pick**, announces | Reply + DM host |
| **Winner payout** | DM → bank details → verify → YES → transfer | Inbound + outbound DMs |
| **Payout confirm** | SafeHaven `account.debit` / Paystack transfer webhooks | — |

Bot loop: `BOT_CYCLE_SECONDS` (default 90s). Set `AUTO_PICK_WINNERS=true` (default) for automatic draws.

---

## Environment essentials

```env
FINTECH_MODE=true
AUTO_PICK_WINNERS=true
ENABLE_BOT_REPLIES=true          # public @winner announcements
PUBLIC_BASE_URL=https://your-app.example.com

# Mocks for local dev
SAFEHAVEN_MOCK=true
PAYSTACK_MOCK=true
```

Production: set real SafeHaven/Paystack keys and `SAFEHAVEN_WEBHOOK_SECRET` + `REQUIRE_WEBHOOK_SECRET=true`.

---

## X Developer Portal

1. App with **Read and write** + **Direct message** (read + write).
2. Regenerate access token after permission changes.
3. Bot account DMs must be open for winner payout collection.

Test connectivity:

```bash
python scripts/test_x_auth.py
curl http://localhost:8000/health/x
python scripts/sandbox_smoke.py    # X + SafeHaven + Paystack
```

---

## Local run

### Option A — venv

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill X credentials
python -m alembic upgrade head
./run.sh
```

### Option B — Docker

```bash
cp .env.example .env
docker compose up --build
# Dashboard: http://localhost:8000
```

---

## Test funding (mock)

```bash
# 1. Trigger intake (or create via dashboard /giveaways/new)
# 2. Simulate webhook (₦5000 prize + ₦300 fee = ₦5300):
curl -X POST http://localhost:8000/webhooks/safehaven/virtual-account \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "virtualAccount.transfer",
    "data": {
      "externalReference": "gw-<GIVEAWAY_UUID>",
      "amount": 5300,
      "paymentReference": "TEST-001",
      "status": "Completed"
    }
  }'
```

---

## Test payout confirmation (mock)

After a winner payout is initiated with reference `payout-<id>-<hex>`:

```bash
curl -X POST http://localhost:8000/webhooks/safehaven/virtual-account \
  -H 'Content-Type: application/json' \
  -d '{
    "eventType": "account.debit",
    "data": {
      "paymentReference": "payout-<WINNER_REF>",
      "status": "Completed",
      "amount": 2500,
      "type": "Outwards"
    }
  }'
```

---

## Dashboard URLs

| URL | Purpose |
|-----|---------|
| `/` | Overview |
| `/giveaways/new` | Create giveaway + VA (fintech mode) |
| `/giveaways/{id}` | Entries, funding, refunds, payouts |
| `/admin/settings` | Fees (fixed / % / both) + Paystack toggle |
| `/payments` | Webhook audit log |
| `/dm-queue` | Outbound DM batch queue |
| `/health` | Scheduler + queue status |

---

## Webhooks (production)

| Endpoint | Events |
|----------|--------|
| `POST /webhooks/safehaven/virtual-account` | VA funding, `account.credit`, `account.debit` (payouts) |
| `POST /webhooks/paystack` | `charge.success`, transfer funding, `transfer.success/failed/reversed` (payouts) |

Paystack: requires valid `x-paystack-signature`. SafeHaven: optional `SAFEHAVEN_WEBHOOK_SECRET`.

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Winners not announced publicly | Set `ENABLE_BOT_REPLIES=true` |
| Payout stuck on `processing` | Check `/payments` for webhook; verify provider callback URL |
| Inbound winner DMs ignored | X app needs DM **read** permission |
| Scheduler idle on Render | Use worker service (`python -m app.worker`) or cron wake |

---

## Commands

```bash
./run.sh
pytest tests/ -v
docker compose up --build
python scripts/sandbox_smoke.py
```