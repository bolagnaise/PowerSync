"""add manual charge fields

Revision ID: o8h9i0j1k2l3
Revises: n7g8h9i0j1k2
Create Date: 2024-12-20

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'o8h9i0j1k2l3'
down_revision = 'n7g8h9i0j1k2'
branch_labels = None
depends_on = None


def upgrade():
    # Add manual charge mode fields to user table
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('manual_charge_active', sa.Boolean(), nullable=True, default=False))
        batch_op.add_column(sa.Column('manual_charge_expires_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('manual_charge_saved_tariff_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_user_manual_charge_saved_tariff',
            'saved_tou_profile',
            ['manual_charge_saved_tariff_id'],
            ['id'],
            use_alter=True
        )


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('fk_user_manual_charge_saved_tariff', type_='foreignkey')
        batch_op.drop_column('manual_charge_saved_tariff_id')
        batch_op.drop_column('manual_charge_expires_at')
        batch_op.drop_column('manual_charge_active')
