"""Add export boost threshold field

Revision ID: j3c4d5e6f7g8
Revises: i2b3c4d5e6f7
Create Date: 2025-12-16

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'j3c4d5e6f7g8'
down_revision = 'i2b3c4d5e6f7'
branch_labels = None
depends_on = None


def upgrade():
    # Add export boost activation threshold field
    # This field determines the minimum actual price for boost to apply
    # (0 = always apply boost)
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('export_boost_threshold', sa.Float(), server_default='0.0'))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('export_boost_threshold')
