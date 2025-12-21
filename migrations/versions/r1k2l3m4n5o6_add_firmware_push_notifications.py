"""Add firmware tracking and push notification fields

Revision ID: r1k2l3m4n5o6
Revises: q0j1k2l3m4n5
Create Date: 2024-12-21 23:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'r1k2l3m4n5o6'
down_revision = 'q0j1k2l3m4n5'
branch_labels = None
depends_on = None


def upgrade():
    # Firmware tracking
    op.add_column('user', sa.Column('powerwall_firmware_version', sa.String(50), nullable=True))
    op.add_column('user', sa.Column('powerwall_firmware_updated', sa.DateTime(), nullable=True))

    # Push notifications
    op.add_column('user', sa.Column('apns_device_token', sa.String(200), nullable=True))
    op.add_column('user', sa.Column('push_notifications_enabled', sa.Boolean(), server_default='1', nullable=True))
    op.add_column('user', sa.Column('notify_firmware_updates', sa.Boolean(), server_default='1', nullable=True))


def downgrade():
    op.drop_column('user', 'notify_firmware_updates')
    op.drop_column('user', 'push_notifications_enabled')
    op.drop_column('user', 'apns_device_token')
    op.drop_column('user', 'powerwall_firmware_updated')
    op.drop_column('user', 'powerwall_firmware_version')
