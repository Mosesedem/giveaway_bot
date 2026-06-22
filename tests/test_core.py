"""Core fintech logic tests (mock mode, no live APIs)."""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("SAFEHAVEN_MOCK", "true")
os.environ.setdefault("PAYSTACK_MOCK", "true")
os.environ.setdefault("AUTO_PICK_WINNERS", "true")

from app.db import SessionLocal, init_db
from app.models import Entry, Giveaway, GiveawayStatus, Winner, WinnerStatus
from app.payments.money import parse_ngn_to_kobo, transaction_fee_from_config
from app.payments.settings import compute_transaction_fee_kobo, seed_default_settings
from app.payments.banks import resolve_bank_code
from app.payments.webhooks import handle_safehaven_webhook
from app.conversation.intake import handle_giveaway_mention
from app.giveaway_lifecycle import auto_close_expired, auto_finalize_closed_giveaway, closes_at_passed
from app.bot_logic import pick_winners


@pytest.fixture
def db():
    init_db()
    session = SessionLocal()
    seed_default_settings(session)
    yield session
    session.close()


def test_fee_default_percent_plus_fixed(db):
    assert compute_transaction_fee_kobo(500_000, db) == 30_000  # 2% + ₦200


def test_fee_modes():
    assert transaction_fee_from_config(100_000, "fixed", 20_000, 2) == 20_000
    assert transaction_fee_from_config(100_000, "percent", 0, 5) == 5_000
    assert transaction_fee_from_config(100_000, "percent_plus_fixed", 20_000, 2) == 22_000


def test_parse_amount():
    assert parse_ngn_to_kobo("5000") == 500_000
    assert parse_ngn_to_kobo("50k") == 5_000_000


def test_bank_resolve():
    assert resolve_bank_code("GTBank") == "058"
    assert resolve_bank_code("zenith bank") == "057"


def test_funding_and_auto_pick(db):
    uid = uuid.uuid4().hex[:6]
    _, g = handle_giveaway_mention(
        db,
        f"host{uid}",
        f"t{uid}",
        f"c{uid}",
        "@bot giveaway 5000 winners: 2 duration: 48h",
        "host",
    )
    assert g.amount_kobo == 530_000

    _, action = handle_safehaven_webhook(
        db,
        {
            "type": "virtualAccount.transfer",
            "data": {
                "externalReference": f"gw-{g.id}",
                "amount": 5300,
                "paymentReference": f"P{uid}",
                "status": "Completed",
            },
        },
    )
    assert action == "activated"
    db.refresh(g)
    assert g.status == GiveawayStatus.ACTIVE

    g.closes_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    assert closes_at_passed(g)

    e1 = Entry(giveaway_id=g.id, tweet_id=f"e1{uid}", user_id="u1", is_valid=True)
    e2 = Entry(giveaway_id=g.id, tweet_id=f"e2{uid}", user_id="u2", is_valid=True)
    db.add_all([e1, e2])
    db.commit()

    assert auto_close_expired(db, g)
    client = MagicMock()

    def enqueue(d, giveaway):
        return 0

    result = auto_finalize_closed_giveaway(
        db,
        client,
        g,
        pick_fn=lambda d, giveaway, client=None: pick_winners(d, giveaway, client=client, announce=False),
        enqueue_dms_fn=enqueue,
    )
    assert result["winners"] == 2
    db.refresh(g)
    assert g.status == GiveawayStatus.WINNERS_SELECTED