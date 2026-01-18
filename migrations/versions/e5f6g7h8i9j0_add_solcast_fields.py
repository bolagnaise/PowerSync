"""Add Solcast solar forecasting fields

Revision ID: e5f6g7h8i9j0
Revises: d4e5f6g7h8i9
Create Date: 2026-01-18 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6g7h8i9j0'
down_revision = 'd4e5f6g7h8i9'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def table_exists(table_name):
    """Check if a table exists."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade():
    # Add Solcast configuration fields to User table
    if not column_exists('user', 'solcast_api_key_encrypted'):
        op.add_column('user', sa.Column('solcast_api_key_encrypted', sa.LargeBinary(), nullable=True))
    if not column_exists('user', 'solcast_resource_id'):
        op.add_column('user', sa.Column('solcast_resource_id', sa.String(50), nullable=True))
    if not column_exists('user', 'solcast_enabled'):
        op.add_column('user', sa.Column('solcast_enabled', sa.Boolean(), default=False))

    # Optional: Store capacity for validation (can also get from Solcast API)
    if not column_exists('user', 'solcast_capacity_kw'):
        op.add_column('user', sa.Column('solcast_capacity_kw', sa.Float(), nullable=True))

    # Create SolcastForecast table for caching forecasts
    if not table_exists('solcast_forecast'):
        op.create_table('solcast_forecast',
            sa.Column('id', sa.Integer(), nullable=False),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('period_end', sa.DateTime(), nullable=False),
            sa.Column('pv_estimate', sa.Float(), nullable=True),  # kW - 50th percentile (most likely)
            sa.Column('pv_estimate10', sa.Float(), nullable=True),  # kW - 10th percentile (conservative)
            sa.Column('pv_estimate90', sa.Float(), nullable=True),  # kW - 90th percentile (optimistic)
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
            sa.PrimaryKeyConstraint('id')
        )
        op.create_index(op.f('ix_solcast_forecast_user_id'), 'solcast_forecast', ['user_id'], unique=False)
        op.create_index(op.f('ix_solcast_forecast_period_end'), 'solcast_forecast', ['period_end'], unique=False)


def downgrade():
    # Drop SolcastForecast table
    if table_exists('solcast_forecast'):
        op.drop_index(op.f('ix_solcast_forecast_period_end'), table_name='solcast_forecast')
        op.drop_index(op.f('ix_solcast_forecast_user_id'), table_name='solcast_forecast')
        op.drop_table('solcast_forecast')

    # Remove Solcast fields from User table
    if column_exists('user', 'solcast_capacity_kw'):
        op.drop_column('user', 'solcast_capacity_kw')
    if column_exists('user', 'solcast_enabled'):
        op.drop_column('user', 'solcast_enabled')
    if column_exists('user', 'solcast_resource_id'):
        op.drop_column('user', 'solcast_resource_id')
    if column_exists('user', 'solcast_api_key_encrypted'):
        op.drop_column('user', 'solcast_api_key_encrypted')
