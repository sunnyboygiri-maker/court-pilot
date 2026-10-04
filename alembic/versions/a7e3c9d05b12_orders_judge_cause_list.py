"""orders, judge name, cause list, screenshot checks

Revision ID: a7e3c9d05b12
Revises: 8c41d2a7f3b1
Create Date: 2026-10-04 13:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7e3c9d05b12'
down_revision: Union[str, None] = '8c41d2a7f3b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('court_cases', sa.Column('judge_name', sa.String(length=255), nullable=True))
    op.add_column('court_cases', sa.Column('court_ref', sa.JSON(), nullable=True))
    op.add_column('court_cases', sa.Column('latest_order_text', sa.Text(), nullable=True))
    op.add_column('search_hits', sa.Column('details', sa.JSON(), nullable=True))
    op.create_table(
        'order_documents',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('number', sa.String(length=20), nullable=False),
        sa.Column('order_date', sa.Date(), nullable=True),
        sa.Column('pdf', sa.LargeBinary(), nullable=False),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['court_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('case_id', 'number', name='uq_order_document'),
    )
    op.create_index(op.f('ix_order_documents_case_id'), 'order_documents', ['case_id'], unique=False)
    op.create_table(
        'cause_listings',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('listing_date', sa.Date(), nullable=False),
        sa.Column('serial', sa.Integer(), nullable=True),
        sa.Column('purpose', sa.String(length=255), nullable=True),
        sa.Column('category', sa.String(length=255), nullable=True),
        sa.Column('judge', sa.String(length=255), nullable=True),
        sa.Column('vc_url', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['case_id'], ['court_cases.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('case_id', 'listing_date', name='uq_cause_listing'),
    )
    op.create_index(op.f('ix_cause_listings_case_id'), 'cause_listings', ['case_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_cause_listings_case_id'), table_name='cause_listings')
    op.drop_table('cause_listings')
    op.drop_index(op.f('ix_order_documents_case_id'), table_name='order_documents')
    op.drop_table('order_documents')
    op.drop_column('search_hits', 'details')
    op.drop_column('court_cases', 'latest_order_text')
    op.drop_column('court_cases', 'court_ref')
    op.drop_column('court_cases', 'judge_name')
