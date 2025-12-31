"""Add inverter curtailment fields

Revision ID: u4n5o6p7q8r9
Revises: t3m4n5o6p7q8
Create Date: 2025-12-31

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'u4n5o6p7q8r9'
down_revision = 't3m4n5o6p7q8'
branch_labels = None
depends_on = None


def upgrade():
    # Add AC-coupled inverter curtailment columns
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('inverter_curtailment_enabled', sa.Boolean(), nullable=True, default=False))
        batch_op.add_column(sa.Column('inverter_brand', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('inverter_model', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('inverter_host', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('inverter_port', sa.Integer(), nullable=True, default=502))
        batch_op.add_column(sa.Column('inverter_slave_id', sa.Integer(), nullable=True, default=1))
        batch_op.add_column(sa.Column('inverter_last_state', sa.String(20), nullable=True))
        batch_op.add_column(sa.Column('inverter_last_state_updated', sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('inverter_last_state_updated')
        batch_op.drop_column('inverter_last_state')
        batch_op.drop_column('inverter_slave_id')
        batch_op.drop_column('inverter_port')
        batch_op.drop_column('inverter_host')
        batch_op.drop_column('inverter_model')
        batch_op.drop_column('inverter_brand')
        batch_op.drop_column('inverter_curtailment_enabled')
