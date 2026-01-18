"""Add weather location fields for weather-based automations

Revision ID: a1t2u3v4w5x6
Revises: z9s0t1u2v3w4
Create Date: 2026-01-18 10:30:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1t2u3v4w5x6'
down_revision = 'z9s0t1u2v3w4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('weather_location', sa.String(100), nullable=True))
        batch_op.add_column(sa.Column('weather_latitude', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('weather_longitude', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('openweathermap_api_key', sa.String(64), nullable=True))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('openweathermap_api_key')
        batch_op.drop_column('weather_longitude')
        batch_op.drop_column('weather_latitude')
        batch_op.drop_column('weather_location')
