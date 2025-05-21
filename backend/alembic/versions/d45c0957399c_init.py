"""init

Revision ID: d45c0957399c
Revises: 82b4c64b470e
Create Date: 2025-05-20 11:37:49.626152

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd45c0957399c'
down_revision: Union[str, None] = '82b4c64b470e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'parking_spot_config',
        sa.Column('id', sa.Integer(), nullable=False, primary_key=True, index=True, autoincrement=True),
        sa.Column('spot_label', sa.String(), nullable=False, index=True),
        sa.Column('camera_id', sa.String(), nullable=False, server_default='default_camera', index=True),
        sa.Column('x_coord', sa.Integer(), nullable=False),
        sa.Column('y_coord', sa.Integer(), nullable=False),
        sa.Column('width', sa.Integer(), nullable=False),
        sa.Column('height', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    )

def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('parking_spot_config')
