"""RSW 정비 안내 MVP 초기 스키마 (43개 테이블, 트리거, 인덱스).

설계 원본: docs/design/schema/schema.sql (v1, 2026-09-26). 빈 DB에만 적용한다.

Revision ID: 0001_rsw_schema
Revises:
Create Date: 2026-09-30
"""
from alembic import op

from app.db.sql_script import load_statements

revision = "0001_rsw_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in load_statements("rsw_schema.sql"):
        op.execute(statement)


def downgrade() -> None:
    # 원문·지식·실행 이력을 삭제하는 경로이므로 개발 DB에서만 사용한다.
    op.execute("DROP SCHEMA rsw CASCADE")
