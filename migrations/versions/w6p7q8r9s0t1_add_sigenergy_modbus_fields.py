"""Add Sigenergy DC curtailment Modbus fields

Revision ID: w6p7q8r9s0t1
Revises: v5o6p7q8r9s0
Create Date: 2024-12-31 23:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'w6p7q8r9s0t1'
down_revision = 'v5o6p7q8r9s0'
branch_labels = None
depends_on = None


def upgrade():
    # Add Sigenergy DC Curtailment Modbus fields
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sigenergy_dc_curtailment_enabled', sa.Boolean(), nullable=True, server_default='0'))
        batch_op.add_column(sa.Column('sigenergy_modbus_host', sa.String(50), nullable=True))
        batch_op.add_column(sa.Column('sigenergy_modbus_port', sa.Integer(), nullable=True, server_default='502'))
        batch_op.add_column(sa.Column('sigenergy_modbus_slave_id', sa.Integer(), nullable=True, server_default='1'))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('sigenergy_modbus_slave_id')
        batch_op.drop_column('sigenergy_modbus_port')
        batch_op.drop_column('sigenergy_modbus_host')
        batch_op.drop_column('sigenergy_dc_curtailment_enabled')
