"""Add battery health history table and install date

Revision ID: p9i0j1k2l3m4
Revises: o8h9i0j1k2l3
Create Date: 2025-12-21

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'p9i0j1k2l3m4'
down_revision = 'o8h9i0j1k2l3'
branch_labels = None
depends_on = None


def upgrade():
    # Add powerwall_install_date to user table
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('powerwall_install_date', sa.Date(), nullable=True))

    # Create battery_health_history table
    op.create_table('battery_health_history',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('scanned_at', sa.DateTime(), nullable=False),
        sa.Column('rated_capacity_wh', sa.Float(), nullable=False),
        sa.Column('actual_capacity_wh', sa.Float(), nullable=False),
        sa.Column('health_percent', sa.Float(), nullable=False),
        sa.Column('degradation_percent', sa.Float(), nullable=False),
        sa.Column('battery_count', sa.Integer(), nullable=False),
        sa.Column('pack_data', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_battery_health_history_scanned_at'), 'battery_health_history', ['scanned_at'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_battery_health_history_scanned_at'), table_name='battery_health_history')
    op.drop_table('battery_health_history')

    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('powerwall_install_date')
