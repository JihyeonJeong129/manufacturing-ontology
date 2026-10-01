# rsw 스키마는 SQL 파일(app/db/sql/*.sql)이 기준이며 alembic 마이그레이션이 그대로 적용한다.
# 43개 테이블의 복합 FK·트리거·도메인을 ORM에 중복 정의하지 않고, 서비스는 명시적 SQL(app/db/sqlutil.py)을 사용한다.
# 따라서 alembic autogenerate는 사용하지 않는다 (alembic/env.py의 target_metadata=None).
from app.db.base import Base  # noqa: F401
