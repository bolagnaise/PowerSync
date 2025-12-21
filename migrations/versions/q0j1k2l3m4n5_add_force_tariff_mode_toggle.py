"""Add force tariff mode toggle option

Revision ID: q0j1k2l3m4n5
Revises: p9i0j1k2l3m4
Create Date: 2025-12-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'q0j1k2l3m4n5'
down_revision = 'p9i0j1k2l3m4'
branch_labels = None
depends_on = None


def upgrade():
    # Add force_tariff_mode_toggle to user table
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('force_tariff_mode_toggle', sa.Boolean(), nullable=True, default=False))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('force_tariff_mode_toggle')
