import os
import uuid

import pytest

os.environ.setdefault("SAFEHAVEN_MOCK", "true")
os.environ.setdefault("PAYSTACK_MOCK", "true")

from app.db import SessionLocal, init_db
from app.models import Entry, Giveaway, PaymentEvent, PayoutStatus, Winner, WinnerStatus
from app.payments.payout_webhooks import (
    handle_paystack_transfer_event,
    handle_safehaven_account_debit,
)


@pytest.fixture
def db():
    init_db()
    session = SessionLocal()
    yield session
    session.close()


def _winner(db, ref: str) -> Winner:
    g = Giveaway(title="Test", num_winners=1, prize_pool_kobo=100_000, amount_kobo=102_000)
    db.add(g)
    db.commit()
    e = Entry(giveaway_id=g.id, tweet_id=f"t-{ref}", user_id="u1", is_valid=True)
    db.add(e)
    db.commit()
    w = Winner(
        giveaway_id=g.id,
        entry_id=e.id,
        user_id="u1",
        payout_reference=ref,
        payout_status=PayoutStatus.PROCESSING,
        status=WinnerStatus.NOTIFIED,
    )
    db.add(w)
    db.commit()
    return w


def test_safehaven_debit_confirms_payout(db):
    ref = f"payout-{uuid.uuid4().hex[:8]}"
    w = _winner(db, ref)
    entity_id, action, kind = handle_safehaven_account_debit(
        db,
        {
            "eventType": "account.debit",
            "data": {
                "paymentReference": ref,
                "status": "Completed",
                "amount": 1000,
                "type": "Outwards",
            },
        },
    )
    assert kind == "winner"
    assert action == "paid"
    assert entity_id == w.id
    db.refresh(w)
    assert w.payout_status.value == "paid"


def test_webhook_confirms_after_processing_event(db):
    """Transfer API logs payout.processing; webhook must still mark PAID."""
    ref = f"payout-{uuid.uuid4().hex[:8]}"
    w = _winner(db, ref)
    db.add(
        PaymentEvent(
            provider="safehaven",
            event_type="payout.processing",
            external_reference=ref,
            payment_reference=ref,
            giveaway_id=w.giveaway_id,
            winner_id=w.id,
            amount_kobo=100_000,
            raw_json="{}",
        )
    )
    db.commit()

    entity_id, action, kind = handle_safehaven_account_debit(
        db,
        {
            "eventType": "account.debit",
            "data": {
                "paymentReference": ref,
                "status": "Completed",
                "amount": 1000,
                "type": "Outwards",
            },
        },
    )
    assert kind == "winner"
    assert action == "paid"
    assert entity_id == w.id
    db.refresh(w)
    assert w.payout_status.value == "paid"


def test_paystack_transfer_failed(db):
    ref = f"payout-{uuid.uuid4().hex[:8]}"
    w = _winner(db, ref)
    entity_id, action, kind = handle_paystack_transfer_event(
        db,
        {
            "event": "transfer.failed",
            "data": {"reference": ref, "status": "failed", "amount": 100000},
        },
    )
    assert kind == "winner"
    assert action == "failed"
    db.refresh(w)
    assert w.payout_status.value == "failed"