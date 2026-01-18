"""Add EV automation trigger fields

Revision ID: c3d4e5f6g7h8
Revises: b2c3d4e5f6g7
Create Date: 2026-01-18 14:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c3d4e5f6g7h8'
down_revision = 'b2c3d4e5f6g7'
branch_labels = None
depends_on = None


def column_exists(table_name, column_name):
    """Check if a column exists in a table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    return column_name in columns


def constraint_exists(table_name, constraint_name):
    """Check if a foreign key constraint exists."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    fks = inspector.get_foreign_keys(table_name)
    return any(fk.get('name') == constraint_name for fk in fks)


def upgrade():
    # Add EV trigger fields to automation_trigger table (if they don't exist)
    if not column_exists('automation_trigger', 'ev_vehicle_id'):
        op.add_column('automation_trigger', sa.Column('ev_vehicle_id', sa.Integer(), nullable=True))
    if not column_exists('automation_trigger', 'ev_condition'):
        op.add_column('automation_trigger', sa.Column('ev_condition', sa.String(30), nullable=True))
    if not column_exists('automation_trigger', 'ev_soc_threshold'):
        op.add_column('automation_trigger', sa.Column('ev_soc_threshold', sa.Integer(), nullable=True))

    # Add foreign key constraint (if it doesn't exist)
    if not constraint_exists('automation_trigger', 'fk_automation_trigger_ev_vehicle'):
        try:
            op.create_foreign_key(
                'fk_automation_trigger_ev_vehicle',
                'automation_trigger',
                'tesla_vehicle',
                ['ev_vehicle_id'],
                ['id']
            )
        except Exception:
            # SQLite doesn't support adding FK constraints to existing tables
            pass


def downgrade():
    # Remove foreign key constraint (if it exists)
    if constraint_exists('automation_trigger', 'fk_automation_trigger_ev_vehicle'):
        try:
            op.drop_constraint('fk_automation_trigger_ev_vehicle', 'automation_trigger', type_='foreignkey')
        except Exception:
            pass

    # Remove EV trigger fields (if they exist)
    if column_exists('automation_trigger', 'ev_soc_threshold'):
        op.drop_column('automation_trigger', 'ev_soc_threshold')
    if column_exists('automation_trigger', 'ev_condition'):
        op.drop_column('automation_trigger', 'ev_condition')
    if column_exists('automation_trigger', 'ev_vehicle_id'):
        op.drop_column('automation_trigger', 'ev_vehicle_id')
