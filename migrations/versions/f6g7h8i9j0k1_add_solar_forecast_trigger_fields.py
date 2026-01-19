"""Add solar forecast trigger fields

Revision ID: f6g7h8i9j0k1
Revises: e5f6g7h8i9j0
Create Date: 2026-01-19 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6g7h8i9j0k1'
down_revision = 'e5f6g7h8i9j0'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    # Add solar forecast trigger fields to automation_trigger table
    if not column_exists('automation_trigger', 'solar_forecast_period'):
        op.add_column('automation_trigger',
            sa.Column('solar_forecast_period', sa.String(20), nullable=True))

    if not column_exists('automation_trigger', 'solar_forecast_condition'):
        op.add_column('automation_trigger',
            sa.Column('solar_forecast_condition', sa.String(20), nullable=True))

    if not column_exists('automation_trigger', 'solar_forecast_threshold_kwh'):
        op.add_column('automation_trigger',
            sa.Column('solar_forecast_threshold_kwh', sa.Float, nullable=True))


def downgrade():
    # Remove solar forecast trigger fields
    if column_exists('automation_trigger', 'solar_forecast_threshold_kwh'):
        op.drop_column('automation_trigger', 'solar_forecast_threshold_kwh')

    if column_exists('automation_trigger', 'solar_forecast_condition'):
        op.drop_column('automation_trigger', 'solar_forecast_condition')

    if column_exists('automation_trigger', 'solar_forecast_period'):
        op.drop_column('automation_trigger', 'solar_forecast_period')
