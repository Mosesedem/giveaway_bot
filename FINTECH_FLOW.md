# Fintech Giveaway Flow — SafeHaven + Paystack

End-to-end money flow for the Nigerian fintech giveaway product.

## Architecture

| Layer | Provider | Role |
|-------|----------|------|
| Host funding | **SafeHaven** (primary) → **Paystack** (auto-failover) | One virtual account **per giveaway** — the prize pool |
| Winner verification | SafeHaven name enquiry → Paystack resolve (failover) | Bank-app style account name check |
| Winner payout | SafeHaven transfer → Paystack transfer (failover) | Debited from the **giveaway VA** (`payout_source_account`) |
| Admin toggle | `/admin/settings` | Prefer Paystack as primary for payouts when enabled |

Set `SAFEHAVEN_MOCK=true` and `PAYSTACK_MOCK=true` for local dev without live keys.

---

## Host flow (fund the giveaway)

```
1. Host tweets:  @YourBot giveaway ₦5,000 / winners: 3 / duration: 7 days

2. Bot collects missing fields if incomplete (amount, winners, duration)

3. Bot creates a per-giveaway VA:
   - Prize pool: ₦5,000
   - Platform fee: **2% + ₦200** by default (configurable at `/admin/settings`)
   - Total due: ₦5,300

4. Host transfers ₦5,300 to the giveaway VA

5. Webhook → giveaway ACTIVE + receipt reply on thread
```

### Incomplete requests

The bot keeps a `conversation_sessions` row and asks for:

- **amount** — `₦5000`, `50k`
- **winners** — `winners: 3`
- **duration** — `duration: 7 days`, `48h`, `closes: 3d`

### Underpay / overpay

If the host sends the wrong amount:

1. Bot **replies on the thread** and **DMs the host**
2. Host chooses:
   - **PROCEED** — run giveaway with received amount (prize pool = received − fee)
   - **RESTRUCTURE** — if overpaid, bot collects bank details and **refunds the excess** first, then issues a **new VA**

---

## Entrant flow

After funding, status = `active`. Replies in the thread are collected until `closes_at` (from host duration).

When `closes_at` passes, the bot auto-closes entries, **randomly picks winners**, announces on the thread, and queues payout DMs (`AUTO_PICK_WINNERS=true` by default).

---

## Winner payout flow

```
1. Bot DMs winner with prize amount + bank format

2. Winner replies: Bank: GTBank / Account: 0123456789

3. Name enquiry (SafeHaven → Paystack fallback)

4. Winner replies YES → transfer from giveaway VA pool

5. Status → paid
```

---

## Webhooks

| Endpoint | Source |
|----------|--------|
| `POST /webhooks/safehaven/virtual-account` | SafeHaven `virtualAccount.transfer` |
| `POST /webhooks/paystack` | Funding: `charge.success`, inbound `transfer.success` · Payout: `transfer.success/failed/reversed` |
| Payout confirmation | SafeHaven `account.debit` (Outwards) · matches `payout-*` / `refund-*` references |

**Security**

- Paystack: `x-paystack-signature` HMAC-SHA512 (required when not in mock mode)
- SafeHaven: optional `SAFEHAVEN_WEBHOOK_SECRET` via `Authorization: Bearer` or `X-Webhook-Secret`
- Idempotency: duplicate `payment_reference` events are ignored

### Simulate funding locally

```bash
# Exact payment (prize ₦5000 + ₦300 fee = ₦5300):
curl -X POST http://localhost:8000/webhooks/safehaven/virtual-account \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "virtualAccount.transfer",
    "data": {
      "externalReference": "gw-<GIVEAWAY_UUID>",
      "amount": 5300,
      "paymentReference": "TEST-REF-001",
      "status": "Completed"
    }
  }'
```

### Simulate underpay (host confirmation)

```bash
curl -X POST http://localhost:8000/webhooks/safehaven/virtual-account \
  -H 'Content-Type: application/json' \
  -d '{
    "data": {
      "externalReference": "gw-<GIVEAWAY_UUID>",
      "amount": 5000,
      "paymentReference": "TEST-REF-002",
      "status": "Completed"
    }
  }'
```

Host replies **PROCEED** or **RESTRUCTURE** in the thread.

---

## Required environment variables

```env
FINTECH_MODE=true
PUBLIC_BASE_URL=https://your-bot.onrender.com
TRANSACTION_FEE_MODE=percent_plus_fixed
TRANSACTION_FEE_KOBO=20000
TRANSACTION_FEE_PERCENT=2

SAFEHAVEN_BASE_URL=https://api.sandbox.safehavenmfb.com
SAFEHAVEN_CLIENT_ID=
SAFEHAVEN_CLIENT_ASSERTION=
SAFEHAVEN_DEBIT_ACCOUNT=
SAFEHAVEN_SETTLEMENT_ACCOUNT_NUMBER=
SAFEHAVEN_WEBHOOK_SECRET=
SAFEHAVEN_MOCK=true

PAYSTACK_SECRET_KEY=
PAYSTACK_PREFERRED_BANK=wema-bank
PAYSTACK_MOCK=true
```

---

## Product decisions (confirmed)

1. **One VA per giveaway** — host funds the pool; winners are paid from that account
2. **Anyone can host** — no pre-KYC (optional `TRUSTED_HOST_USER_IDS` restrict)
3. **Fee on top** — host pays prize + fee (e.g. ₦5,000 + ₦150 = ₦5,150)
4. **Paystack** — full automatic failover for VA, verify, and transfer
5. **Duration required** — host sets when entry collection ends / selection can start