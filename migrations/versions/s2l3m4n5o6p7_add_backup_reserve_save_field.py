"""Add manual_charge_saved_backup_reserve field

Revision ID: s2l3m4n5o6p7
Revises: r1k2l3m4n5o6
Create Date: 2025-12-22

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 's2l3m4n5o6p7'
down_revision = 'r1k2l3m4n5o6'
branch_labels = None
depends_on = None


def upgrade():
    # Add field to store backup reserve during force charge
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manual_charge_saved_backup_reserve', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('manual_charge_saved_backup_reserve')
