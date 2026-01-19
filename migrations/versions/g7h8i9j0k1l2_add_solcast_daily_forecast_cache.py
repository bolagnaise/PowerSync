"""Add Solcast daily forecast cache fields

Revision ID: g7h8i9j0k1l2
Revises: f6g7h8i9j0k1
Create Date: 2026-01-19 14:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g7h8i9j0k1l2'
down_revision = 'f6g7h8i9j0k1'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    # Add daily forecast cache fields to User table
    # These store pre-calculated daily totals to avoid recalculating from forecast table
    if not column_exists('user', 'solcast_daily_forecast_date'):
        op.add_column('user', sa.Column('solcast_daily_forecast_date', sa.Date(), nullable=True))
    if not column_exists('user', 'solcast_daily_forecast_kwh'):
        op.add_column('user', sa.Column('solcast_daily_forecast_kwh', sa.Float(), nullable=True))
    if not column_exists('user', 'solcast_daily_forecast_peak_kw'):
        op.add_column('user', sa.Column('solcast_daily_forecast_peak_kw', sa.Float(), nullable=True))


def downgrade():
    # Remove daily forecast cache fields from User table
    if column_exists('user', 'solcast_daily_forecast_peak_kw'):
        op.drop_column('user', 'solcast_daily_forecast_peak_kw')
    if column_exists('user', 'solcast_daily_forecast_kwh'):
        op.drop_column('user', 'solcast_daily_forecast_kwh')
    if column_exists('user', 'solcast_daily_forecast_date'):
        op.drop_column('user', 'solcast_daily_forecast_date')
