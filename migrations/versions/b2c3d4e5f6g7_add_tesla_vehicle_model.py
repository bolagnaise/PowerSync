"""Add TeslaVehicle model for EV charging control

Revision ID: b2c3d4e5f6g7
Revises: 7543363ae4c5
Create Date: 2026-01-18 12:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6g7'
down_revision = '7543363ae4c5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tesla_vehicle',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('vehicle_id', sa.String(50), nullable=False),
        sa.Column('vin', sa.String(20), nullable=True),
        sa.Column('display_name', sa.String(100), nullable=True),
        sa.Column('model', sa.String(50), nullable=True),
        sa.Column('year', sa.Integer(), nullable=True),
        sa.Column('color', sa.String(50), nullable=True),
        sa.Column('charging_state', sa.String(30), nullable=True),
        sa.Column('battery_level', sa.Integer(), nullable=True),
        sa.Column('battery_range', sa.Float(), nullable=True),
        sa.Column('charge_limit_soc', sa.Integer(), nullable=True),
        sa.Column('charge_current_request', sa.Integer(), nullable=True),
        sa.Column('charge_current_actual', sa.Float(), nullable=True),
        sa.Column('charger_voltage', sa.Integer(), nullable=True),
        sa.Column('charger_power', sa.Float(), nullable=True),
        sa.Column('time_to_full_charge', sa.Float(), nullable=True),
        sa.Column('charge_port_door_open', sa.Boolean(), nullable=True),
        sa.Column('charge_port_latch', sa.String(20), nullable=True),
        sa.Column('is_online', sa.Boolean(), default=False),
        sa.Column('is_plugged_in', sa.Boolean(), default=False),
        sa.Column('latitude', sa.Float(), nullable=True),
        sa.Column('longitude', sa.Float(), nullable=True),
        sa.Column('location_name', sa.String(200), nullable=True),
        sa.Column('last_seen', sa.DateTime(), nullable=True),
        sa.Column('data_updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('enable_automations', sa.Boolean(), default=True),
        sa.Column('prioritize_powerwall', sa.Boolean(), default=False),
        sa.Column('powerwall_min_soc', sa.Integer(), default=80),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_tesla_vehicle_user_id'), 'tesla_vehicle', ['user_id'], unique=False)
    op.create_index(op.f('ix_tesla_vehicle_vehicle_id'), 'tesla_vehicle', ['vehicle_id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_tesla_vehicle_vehicle_id'), table_name='tesla_vehicle')
    op.drop_index(op.f('ix_tesla_vehicle_user_id'), table_name='tesla_vehicle')
    op.drop_table('tesla_vehicle')
