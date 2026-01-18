"""Add OCPP charger and transaction tables

Revision ID: d4e5f6g7h8i9
Revises: c3d4e5f6g7h8
Create Date: 2026-01-18 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6g7h8i9'
down_revision = 'c3d4e5f6g7h8'
branch_labels = None
depends_on = None


def upgrade():
    # Create OCPP Charger table
    op.create_table('ocpp_charger',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('charger_id', sa.String(50), nullable=False),
        sa.Column('vendor', sa.String(50), nullable=True),
        sa.Column('model', sa.String(50), nullable=True),
        sa.Column('serial_number', sa.String(50), nullable=True),
        sa.Column('firmware_version', sa.String(50), nullable=True),
        sa.Column('is_connected', sa.Boolean(), default=False),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
        sa.Column('last_boot', sa.DateTime(), nullable=True),
        sa.Column('status', sa.String(20), default='Unavailable'),
        sa.Column('error_code', sa.String(50), nullable=True),
        sa.Column('current_transaction_id', sa.Integer(), nullable=True),
        sa.Column('current_power_kw', sa.Float(), nullable=True),
        sa.Column('current_energy_kwh', sa.Float(), nullable=True),
        sa.Column('current_soc', sa.Integer(), nullable=True),
        sa.Column('meter_value_kwh', sa.Float(), nullable=True),
        sa.Column('max_power_kw', sa.Float(), nullable=True),
        sa.Column('num_connectors', sa.Integer(), default=1),
        sa.Column('display_name', sa.String(100), nullable=True),
        sa.Column('enable_automations', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('charger_id')
    )
    op.create_index(op.f('ix_ocpp_charger_user_id'), 'ocpp_charger', ['user_id'], unique=False)
    op.create_index(op.f('ix_ocpp_charger_charger_id'), 'ocpp_charger', ['charger_id'], unique=True)

    # Create OCPP Transaction table
    op.create_table('ocpp_transaction',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('charger_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), nullable=False),
        sa.Column('id_tag', sa.String(50), nullable=True),
        sa.Column('connector_id', sa.Integer(), default=1),
        sa.Column('start_time', sa.DateTime(), nullable=False),
        sa.Column('stop_time', sa.DateTime(), nullable=True),
        sa.Column('stop_reason', sa.String(50), nullable=True),
        sa.Column('meter_start', sa.Float(), nullable=True),
        sa.Column('meter_stop', sa.Float(), nullable=True),
        sa.Column('energy_kwh', sa.Float(), nullable=True),
        sa.Column('max_power_kw', sa.Float(), nullable=True),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('cost_currency', sa.String(3), nullable=True),
        sa.ForeignKeyConstraint(['charger_id'], ['ocpp_charger.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_ocpp_transaction_charger_id'), 'ocpp_transaction', ['charger_id'], unique=False)
    op.create_index(op.f('ix_ocpp_transaction_user_id'), 'ocpp_transaction', ['user_id'], unique=False)

    # Add OCPP trigger fields to automation_trigger table
    op.add_column('automation_trigger', sa.Column('ocpp_charger_id', sa.Integer(), nullable=True))
    op.add_column('automation_trigger', sa.Column('ocpp_condition', sa.String(30), nullable=True))
    op.add_column('automation_trigger', sa.Column('ocpp_energy_threshold', sa.Float(), nullable=True))

    # Add foreign key constraint for ocpp_charger_id
    op.create_foreign_key(
        'fk_automation_trigger_ocpp_charger',
        'automation_trigger',
        'ocpp_charger',
        ['ocpp_charger_id'],
        ['id']
    )


def downgrade():
    # Remove foreign key constraint
    op.drop_constraint('fk_automation_trigger_ocpp_charger', 'automation_trigger', type_='foreignkey')

    # Remove OCPP trigger fields from automation_trigger
    op.drop_column('automation_trigger', 'ocpp_energy_threshold')
    op.drop_column('automation_trigger', 'ocpp_condition')
    op.drop_column('automation_trigger', 'ocpp_charger_id')

    # Drop OCPP Transaction table
    op.drop_index(op.f('ix_ocpp_transaction_user_id'), table_name='ocpp_transaction')
    op.drop_index(op.f('ix_ocpp_transaction_charger_id'), table_name='ocpp_transaction')
    op.drop_table('ocpp_transaction')

    # Drop OCPP Charger table
    op.drop_index(op.f('ix_ocpp_charger_charger_id'), table_name='ocpp_charger')
    op.drop_index(op.f('ix_ocpp_charger_user_id'), table_name='ocpp_charger')
    op.drop_table('ocpp_charger')
