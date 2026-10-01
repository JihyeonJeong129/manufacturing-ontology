"""API 보조 테이블: background_jobs, api_credentials, api_idempotency_requests,
feedback_one_successor 유일 인덱스 (API_SPEC.md 10장).

Revision ID: 0002_api_support
Revises: 0001_rsw_schema
Create Date: 2026-09-30
"""
from alembic import op

from app.db.sql_script import load_statements

revision = "0002_api_support"
down_revision = "0001_rsw_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in load_statements("api_support.sql"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET search_path = rsw, public")
    op.execute("DROP INDEX IF EXISTS retrieval_candidates_run_lookup")
    op.execute("DROP INDEX IF EXISTS answer_citations_run_lookup")
    op.execute("DROP INDEX IF EXISTS feedback_run_lookup")
    op.execute("DROP INDEX IF EXISTS feedback_one_successor")
    op.execute("DROP TABLE IF EXISTS api_idempotency_requests")
    op.execute("DROP TABLE IF EXISTS api_credentials")
    op.execute("DROP TABLE IF EXISTS background_jobs")
    op.execute("DROP FUNCTION IF EXISTS guard_background_job()")
