"""Add Enphase grid profile fields for fallback profile switching

Revision ID: h8i9j0k1l2m3
Revises: g7h8i9j0k1l2
Create Date: 2026-01-19 16:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h8i9j0k1l2m3'
down_revision = 'g7h8i9j0k1l2'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def upgrade():
    # Add Enphase grid profile fields for fallback profile switching
    # These store the names of grid profiles to switch between when DPEL/DER fail
    if not column_exists('user', 'enphase_normal_profile'):
        op.add_column('user', sa.Column('enphase_normal_profile', sa.String(length=200), nullable=True))
    if not column_exists('user', 'enphase_zero_export_profile'):
        op.add_column('user', sa.Column('enphase_zero_export_profile', sa.String(length=200), nullable=True))


def downgrade():
    # Remove Enphase grid profile fields
    if column_exists('user', 'enphase_zero_export_profile'):
        op.drop_column('user', 'enphase_zero_export_profile')
    if column_exists('user', 'enphase_normal_profile'):
        op.drop_column('user', 'enphase_normal_profile')
