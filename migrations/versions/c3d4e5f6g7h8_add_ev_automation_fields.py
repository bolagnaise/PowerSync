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


def upgrade():
    # Add EV trigger fields to automation_trigger table
    op.add_column('automation_trigger', sa.Column('ev_vehicle_id', sa.Integer(), nullable=True))
    op.add_column('automation_trigger', sa.Column('ev_condition', sa.String(30), nullable=True))
    op.add_column('automation_trigger', sa.Column('ev_soc_threshold', sa.Integer(), nullable=True))

    # Add foreign key constraint
    op.create_foreign_key(
        'fk_automation_trigger_ev_vehicle',
        'automation_trigger',
        'tesla_vehicle',
        ['ev_vehicle_id'],
        ['id']
    )


def downgrade():
    # Remove foreign key constraint
    op.drop_constraint('fk_automation_trigger_ev_vehicle', 'automation_trigger', type_='foreignkey')

    # Remove EV trigger fields
    op.drop_column('automation_trigger', 'ev_soc_threshold')
    op.drop_column('automation_trigger', 'ev_condition')
    op.drop_column('automation_trigger', 'ev_vehicle_id')
