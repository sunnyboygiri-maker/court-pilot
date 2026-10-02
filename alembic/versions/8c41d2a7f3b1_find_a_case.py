"""find a case: case index, my courts, search jobs

Revision ID: 8c41d2a7f3b1
Revises: 5b2f7c1d9e40
Create Date: 2026-10-02 14:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8c41d2a7f3b1'
down_revision: Union[str, None] = '5b2f7c1d9e40'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'case_index',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('cnr_number', sa.String(length=25), nullable=False),
        sa.Column('case_type', sa.String(length=100), nullable=True),
        sa.Column('case_number', sa.String(length=100), nullable=True),
        sa.Column('reg_year', sa.Integer(), nullable=True),
        sa.Column('petitioner', sa.Text(), nullable=True),
        sa.Column('respondent', sa.Text(), nullable=True),
        sa.Column('fir', sa.String(length=50), nullable=True),
        sa.Column('court_name', sa.String(length=255), nullable=True),
        sa.Column('state_code', sa.String(length=10), nullable=True),
        sa.Column('dist_code', sa.String(length=10), nullable=True),
        sa.Column('complex_code', sa.String(length=20), nullable=True),
        sa.Column('name_key', sa.Text(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_case_index_cnr_number'), 'case_index', ['cnr_number'], unique=True)
    op.create_index('ix_case_index_place', 'case_index', ['state_code', 'dist_code', 'complex_code'], unique=False)

    op.create_table(
        'user_courts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('state_code', sa.String(length=10), nullable=False),
        sa.Column('state_name', sa.String(length=100), nullable=False),
        sa.Column('dist_code', sa.String(length=10), nullable=False),
        sa.Column('dist_name', sa.String(length=100), nullable=False),
        sa.Column('complex_value', sa.String(length=255), nullable=False),
        sa.Column('complex_name', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_id', 'complex_value', name='uq_user_court'),
    )
    op.create_index(op.f('ix_user_courts_user_id'), 'user_courts', ['user_id'], unique=False)

    op.create_table(
        'search_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=20), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('total', sa.Integer(), nullable=False),
        sa.Column('done', sa.Integer(), nullable=False),
        sa.Column('failed', sa.Integer(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=True),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_search_jobs_user_id'), 'search_jobs', ['user_id'], unique=False)

    op.create_table(
        'search_hits',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=False),
        sa.Column('cnr_number', sa.String(length=25), nullable=False),
        sa.Column('case_type', sa.String(length=100), nullable=True),
        sa.Column('case_number', sa.String(length=100), nullable=True),
        sa.Column('petitioner', sa.Text(), nullable=True),
        sa.Column('respondent', sa.Text(), nullable=True),
        sa.Column('fir', sa.String(length=50), nullable=True),
        sa.Column('court_name', sa.String(length=255), nullable=True),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['search_jobs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('job_id', 'cnr_number', name='uq_search_hit'),
    )
    op.create_index(op.f('ix_search_hits_job_id'), 'search_hits', ['job_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_search_hits_job_id'), table_name='search_hits')
    op.drop_table('search_hits')
    op.drop_index(op.f('ix_search_jobs_user_id'), table_name='search_jobs')
    op.drop_table('search_jobs')
    op.drop_index(op.f('ix_user_courts_user_id'), table_name='user_courts')
    op.drop_table('user_courts')
    op.drop_index('ix_case_index_place', table_name='case_index')
    op.drop_index(op.f('ix_case_index_cnr_number'), table_name='case_index')
    op.drop_table('case_index')
