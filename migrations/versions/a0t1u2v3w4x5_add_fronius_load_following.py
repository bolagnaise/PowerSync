"""Add fronius_load_following column

Revision ID: a0t1u2v3w4x5
Revises: z9s0t1u2v3w4
Create Date: 2026-01-09 20:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a0t1u2v3w4x5'
down_revision = 'z9s0t1u2v3w4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('fronius_load_following', sa.Boolean(), nullable=True, default=False))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('fronius_load_following')
