"""Add missing columns for smart curtailment and inverter tracking

Revision ID: y8r9s0t1u2v3
Revises: x7q8r9s0t1u2
Create Date: 2026-01-01 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'y8r9s0t1u2v3'
down_revision = 'x7q8r9s0t1u2'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sigenergy_export_limit_kw', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('inverter_power_limit_w', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('inverter_power_limit_w')
        batch_op.drop_column('sigenergy_export_limit_kw')
