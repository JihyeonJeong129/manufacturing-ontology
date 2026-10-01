"""공통 테스트 설정.

단위 테스트는 DB 없이 실행된다. 앱 모듈은 import 시 Settings를 만들므로 필수 DB 환경변수의
기본값만 채운다(연결은 하지 않는다). 통합 테스트 설정은 tests/integration/conftest.py에 있다.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("POSTGRES_USER", "app_user")
os.environ.setdefault("POSTGRES_PASSWORD", "change_me_strong_password")
os.environ.setdefault("POSTGRES_DB", "app_db")
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("STORAGE_ROOT", os.path.join(tempfile.gettempdir(), "rsw-test-storage"))
