"""DB 없이 검사 가능한 HTTP 계약: 인증 헤더, Content-Type, 크기, 엄격한 JSON, 경로 검증, Problem 형식."""
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import Principal
from app.db.session import get_db
from app.main import create_app
from tests.contract import assert_contract, examples

SERVICE = Principal(uuid4(), "service", "svc", uuid4(), uuid4(), "demo-ml")
OPERATOR = Principal(uuid4(), "operator", "op", uuid4(), None, None)


class ExplodingSession:
    """DB에 닿으면 실패한다. 아래 요청들은 DB 접근 전에 거절되어야 한다."""

    def __getattr__(self, name):
        raise AssertionError(f"database must not be used (called {name})")


async def no_db():
    yield ExplodingSession()


@pytest.fixture
def client():
    app = create_app()
    app.dependency_overrides[get_db] = no_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, app


def as_principal(app, principal):
    async def fake():
        return principal
    app.dependency_overrides[deps.get_principal] = fake


def assert_problem(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status and body["code"] == code
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["cache-control"] == "no-store"
    assert_contract(body, "Problem")
    return body


def test_missing_or_malformed_token_is_401(client):
    c, _ = client
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        response = c.get(f"/api/v1/alerts/{uuid4()}", headers=headers)
        assert_problem(response, 401, "UNAUTHORIZED")
        assert response.headers["www-authenticate"] == "Bearer"


def test_invalid_path_uuid_is_422(client):
    c, app = client
    as_principal(app, OPERATOR)
    body = assert_problem(c.get("/api/v1/answers/not-a-uuid"), 422, "VALIDATION_ERROR")
    assert body["errors"][0]["location"] == "path"


def test_alert_post_requires_producer_role(client):
    c, app = client
    as_principal(app, OPERATOR)
    assert_problem(c.post("/api/v1/alerts", json=examples()["alert_request"]), 403, "PRODUCER_NOT_ALLOWED")


@pytest.mark.parametrize("headers, body, status, code", [
    ({"Content-Type": "text/plain"}, b"{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ({"Content-Type": "application/json; charset=latin-1"}, b"{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ({"Content-Type": "application/json", "Content-Encoding": "gzip"}, b"{}", 415, "UNSUPPORTED_MEDIA_TYPE"),
    ({"Content-Type": "application/json"}, b'{"a":1,"a":2}', 400, "INVALID_JSON"),
    ({"Content-Type": "application/json"}, b'{"a": NaN}', 400, "INVALID_JSON"),
    ({"Content-Type": "application/json"}, b"not json", 400, "INVALID_JSON"),
    ({"Content-Type": "application/json"}, b"[]", 422, "VALIDATION_ERROR"),
    ({"Content-Type": "application/json"}, b'{"x": "\\u0000"}', 422, "VALIDATION_ERROR"),
])
def test_alert_body_rules(client, headers, body, status, code):
    c, app = client
    as_principal(app, SERVICE)
    assert_problem(c.post("/api/v1/alerts", content=body, headers=headers), status, code)


def test_alert_body_size_limit(client):
    c, app = client
    as_principal(app, SERVICE)
    big = b'{"pad": "' + b"x" * (1024 * 1024) + b'"}'
    assert_problem(c.post("/api/v1/alerts", content=big, headers={"Content-Type": "application/json"}), 413, "PAYLOAD_TOO_LARGE")


def test_alert_field_errors_use_json_pointers(client):
    c, app = client
    as_principal(app, SERVICE)
    payload = dict(examples()["alert_request"])
    payload["ml_assessments"] = [dict(payload["ml_assessments"][0], score={"kind": "probability", "value": 3, "calibration_status": "unknown"})]
    payload["release_id"] = "forbidden"
    body = assert_problem(c.post("/api/v1/alerts", json=payload), 422, "VALIDATION_ERROR")
    paths = {e["path"] for e in body["errors"]}
    assert "/release_id" in paths
    assert any(p.startswith("/ml_assessments/0/score") for p in paths)


def test_question_requires_idempotency_key(client):
    c, app = client
    as_principal(app, OPERATOR)
    url = f"/api/v1/alerts/{uuid4()}/questions"
    assert_problem(c.post(url, json=examples()["question_request"]), 400, "IDEMPOTENCY_KEY_REQUIRED")
    body = assert_problem(c.post(url, json=examples()["question_request"], headers={"Idempotency-Key": "bad key!"}), 422, "VALIDATION_ERROR")
    assert body["errors"][0]["location"] == "header"
    assert_problem(c.post(url, content=b"{" + b" " * (64 * 1024) + b"}",
                          headers={"Idempotency-Key": "k1", "Content-Type": "application/json"}), 413, "PAYLOAD_TOO_LARGE")


def test_feedback_validation_before_db(client):
    c, app = client
    as_principal(app, OPERATOR)
    url = f"/api/v1/answers/{uuid4()}/feedback"
    body = assert_problem(c.post(url, json={"verdict": "acceptable", "error_categories": ["other"]},
                                 headers={"Idempotency-Key": "k1"}), 422, "VALIDATION_ERROR")
    assert body["errors"]


def test_service_tokens_cannot_use_operator_endpoints(client):
    c, app = client
    as_principal(app, SERVICE)
    assert_problem(c.get(f"/api/v1/answers/{uuid4()}"), 403, "FORBIDDEN")
    assert_problem(c.get(f"/api/v1/documents/{uuid4()}"), 403, "FORBIDDEN")


def test_unknown_route_is_problem_json(client):
    c, _ = client
    assert_problem(c.get("/api/v1/nope"), 404, "NOT_FOUND")


def test_all_contract_operations_are_routed(client):
    _, app = client
    from tests.contract import openapi

    generated = app.openapi()["paths"]
    for path, item in openapi()["paths"].items():
        for method in item:
            assert method in generated.get(path, {}), (method, path)
