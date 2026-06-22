"""admin_fees_refunds_notify

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-22 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.add_column(sa.Column("selection_notified_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("refund_amount_kobo", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "refund_status",
                sa.Enum(
                    "NOT_REQUIRED",
                    "COLLECTING_BANK",
                    "PROCESSING",
                    "COMPLETED",
                    "FAILED",
                    name="refundstatus",
                ),
                nullable=False,
                server_default="NOT_REQUIRED",
            )
        )
        batch_op.add_column(sa.Column("refund_reference", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("refund_bank_code", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("refund_account_number", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("refund_account_name", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("giveaways", schema=None) as batch_op:
        batch_op.drop_column("refund_account_name")
        batch_op.drop_column("refund_account_number")
        batch_op.drop_column("refund_bank_code")
        batch_op.drop_column("refund_reference")
        batch_op.drop_column("refund_status")
        batch_op.drop_column("refund_amount_kobo")
        batch_op.drop_column("selection_notified_at")