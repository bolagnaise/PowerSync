"""Add settled_prices_only field for Amber users who only want settled prices synced

Revision ID: n7g8h9i0j1k2
Revises: m6f7g8h9i0j1
Create Date: 2024-12-19

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'n7g8h9i0j1k2'
down_revision = 'm6f7g8h9i0j1'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    result = bind.execute(sa.text(f"PRAGMA table_info({table_name})"))
    columns = [row[1] for row in result]
    return column_name in columns


def upgrade():
    # Add settled_prices_only field (default False - use forecast+settled prices)
    if not column_exists('user', 'settled_prices_only'):
        op.add_column('user', sa.Column('settled_prices_only', sa.Boolean(), nullable=True, server_default='0'))

    # Set default value for existing users (disabled by default)
    op.execute("UPDATE user SET settled_prices_only = 0 WHERE settled_prices_only IS NULL")


def downgrade():
    if column_exists('user', 'settled_prices_only'):
        op.drop_column('user', 'settled_prices_only')
