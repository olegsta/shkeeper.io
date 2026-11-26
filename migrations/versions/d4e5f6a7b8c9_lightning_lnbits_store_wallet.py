"""LNbits wallet per Lightning store

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-11-13

"""
from alembic import op
import sqlalchemy as sa


revision = "d4e5f6a7b8c9"
down_revision = "c3d4e5f6a7b8"
branch_labels = None
depends_on = None

_TABLE = "bitcoin_lightning_store_wallet"


def _inspector():
    return sa.inspect(op.get_bind())


def upgrade():
    if _TABLE in _inspector().get_table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("store_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("wallet_id", sa.String(), nullable=False),
        sa.Column("admin_key", sa.Text(), nullable=False),
        sa.Column("invoice_key", sa.Text(), nullable=False),
        sa.Column("lnurl", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["store_id"], ["store.id"], name="fk_btc_ln_store_wallet_store_id"),
        sa.PrimaryKeyConstraint("store_id"),
    )


def downgrade():
    if _TABLE in _inspector().get_table_names():
        op.drop_table(_TABLE)
