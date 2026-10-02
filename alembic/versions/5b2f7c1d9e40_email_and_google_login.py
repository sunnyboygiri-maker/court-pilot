"""email and google login: phone optional, email_verified flag

Revision ID: 5b2f7c1d9e40
Revises: 01853d9c14ed
Create Date: 2026-10-02 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5b2f7c1d9e40'
down_revision: Union[str, None] = '01853d9c14ed'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('users', 'phone', existing_type=sa.String(length=15), nullable=True)
    # Existing emails were typed in without proof of ownership, so they start unverified
    op.add_column('users', sa.Column('email_verified', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('users', 'email_verified')
    # Fails if any user has no phone; delete or fix those rows first
    op.alter_column('users', 'phone', existing_type=sa.String(length=15), nullable=False)
