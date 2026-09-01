"""add payment segmentation fields

Revision ID: 5a6ff487fe3c
Revises: ed7e2117b4b1
Create Date: 2026-09-01 18:59:06.651905

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5a6ff487fe3c'
down_revision: Union[str, Sequence[str], None] = 'ed7e2117b4b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('payment_events', sa.Column('payment_method', sa.String(length=50), nullable=True))
    op.add_column('payment_events', sa.Column('bank', sa.String(length=50), nullable=True))
    op.add_column('payment_events', sa.Column('wallet', sa.String(length=50), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('payment_events', 'wallet')
    op.drop_column('payment_events', 'bank')
    op.drop_column('payment_events', 'payment_method')
    # ### end Alembic commands ###
