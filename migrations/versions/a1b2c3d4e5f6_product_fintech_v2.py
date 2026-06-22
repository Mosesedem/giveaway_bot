"""product_fintech_v2

Revision ID: a1b2c3d4e5f6
Revises: 139dba0b8c81
Create Date: 2026-06-22 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "139dba0b8c81"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.add_column(sa.Column("prize_pool_kobo", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("transaction_fee_kobo", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("payout_source_account", sa.String(), nullable=True))

    with op.batch_alter_table("payment_events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("payment_reference", sa.String(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_payment_events_payment_reference"),
            ["payment_reference"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("payment_events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_payment_events_payment_reference"))
        batch_op.drop_column("payment_reference")

    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.drop_column("payout_source_account")
        batch_op.drop_column("transaction_fee_kobo")
        batch_op.drop_column("prize_pool_kobo")