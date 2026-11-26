"""Tag Lightning invoices with store_id

Revision ID: c3d4e5f6a7b8
Revises: f1a2b3c4d5e6
Create Date: 2026-11-13

"""
from alembic import op
import sqlalchemy as sa


revision = "c3d4e5f6a7b8"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

_TABLE = "bitcoin_lightning_invoice"
_FK = "fk_bitcoin_lightning_invoice_store_id"


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(name):
    return name in _inspector().get_table_names()


def _column_exists(table, column):
    if not _table_exists(table):
        return False
    return column in {c["name"] for c in _inspector().get_columns(table)}


def _fk_names(table):
    return {fk.get("name") for fk in _inspector().get_foreign_keys(table)}


def upgrade():
    if not _table_exists(_TABLE):
        return
    index_name = "ix_bitcoin_lightning_invoice_store_id"
    indexes = {idx.get("name") for idx in _inspector().get_indexes(_TABLE)}
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        if not _column_exists(_TABLE, "store_id"):
            batch_op.add_column(sa.Column("store_id", sa.Integer(), nullable=True))
        if _FK not in _fk_names(_TABLE):
            batch_op.create_foreign_key(_FK, "store", ["store_id"], ["id"])
        if index_name not in indexes:
            batch_op.create_index(index_name, ["store_id"], unique=False)


def downgrade():
    if not _table_exists(_TABLE) or not _column_exists(_TABLE, "store_id"):
        return
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_index("ix_bitcoin_lightning_invoice_store_id")
        if _FK in _fk_names(_TABLE):
            batch_op.drop_constraint(_FK, type_="foreignkey")
        batch_op.drop_column("store_id")
