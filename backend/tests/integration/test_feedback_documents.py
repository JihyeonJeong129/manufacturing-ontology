"""피드백 정정 규칙과 PDF 제공 (ETag/Range/무결성)."""
import hashlib
import uuid

from app.db.sqlutil import execute, fetch_one
from app.demo.pdf import make_text_pdf
from tests.contract import assert_contract
from tests.integration.helpers import ask, ask_and_answer, ready_alert


async def _post_feedback(client, headers, answer_id, body, key=None):
    response = await client.post(f"/api/v1/answers/{answer_id}/feedback", json=body,
                                 headers={**headers, "Idempotency-Key": key or f"f-{uuid.uuid4().hex}"})
    if response.status_code == 201:
        assert_contract(response.json(), "FeedbackCreated")
    else:
        assert_contract(response.json(), "Problem")
    return response


async def test_feedback_create_replay_and_supersede(client, tokens, worker, database):
    alert = await ready_alert(client, tokens, worker)
    done = await ask_and_answer(client, tokens, worker, alert)
    body = {"verdict": "needs_revision", "error_categories": ["missing_context"], "comment": "제어기 모델 확인도 요청", "supersedes_id": None}
    key = f"f-{uuid.uuid4().hex}"
    first = await _post_feedback(client, tokens["operator"], done["answer_id"], body, key)
    assert first.status_code == 201 and first.headers["idempotency-replayed"] == "false"
    replay = await _post_feedback(client, tokens["operator"], done["answer_id"], body, key)
    assert replay.status_code == 201 and replay.json() == first.json() and replay.headers["idempotency-replayed"] == "true"

    fix = {"verdict": "acceptable", "error_categories": [], "supersedes_id": first.json()["feedback_id"]}
    corrected = await _post_feedback(client, tokens["operator"], done["answer_id"], fix)
    assert corrected.status_code == 201 and corrected.json()["supersedes_id"] == first.json()["feedback_id"]
    again = await _post_feedback(client, tokens["operator"], done["answer_id"], fix)
    assert again.status_code == 409 and again.json()["code"] == "FEEDBACK_ALREADY_SUPERSEDED"
    other_author = await _post_feedback(client, tokens["reviewer"], done["answer_id"],
                                        {**fix, "supersedes_id": corrected.json()["feedback_id"]})
    assert other_author.status_code == 403 and other_author.json()["code"] == "FEEDBACK_NOT_OWNED"

    other_answer = await ask_and_answer(client, tokens, worker, alert, question="두 번째 질문")
    wrong_target = await _post_feedback(client, tokens["operator"], other_answer["answer_id"],
                                        {**fix, "supersedes_id": corrected.json()["feedback_id"]})
    assert wrong_target.status_code == 409 and wrong_target.json()["code"] == "FEEDBACK_TARGET_MISMATCH"
    missing = await _post_feedback(client, tokens["operator"], done["answer_id"], {**fix, "supersedes_id": str(uuid.uuid4())})
    assert missing.status_code == 404

    # 피드백은 지식을 바꾸지 않는다: 원인 후보/적용성은 그대로다.
    async with database.session() as s:
        count = await fetch_one(s, "SELECT count(*) AS n FROM rsw.feedback WHERE run_id = :r", r=done["answer_id"])
    assert count["n"] == 2


async def test_feedback_requires_completed_answer(client, tokens, worker):
    alert = await ready_alert(client, tokens, worker)
    pending = await ask(client, tokens, alert)
    response = await _post_feedback(client, tokens["operator"], pending.json()["answer_id"],
                                    {"verdict": "acceptable", "error_categories": []})
    assert response.status_code == 409 and response.json()["code"] == "ANSWER_NOT_REVIEWABLE"
    assert (await _post_feedback(client, tokens["operator"], str(uuid.uuid4()), {"verdict": "acceptable", "error_categories": []})).status_code == 404


async def test_document_download_contract(client, tokens, seed):
    url = f"/api/v1/documents/{seed['document_revision_id']}"
    full = await client.get(url, headers=tokens["operator"])
    assert full.status_code == 200 and full.content.startswith(b"%PDF-")
    assert full.headers["content-type"] == "application/pdf"
    assert full.headers["cache-control"] == "private, no-cache" and full.headers["accept-ranges"] == "bytes"
    assert int(full.headers["content-length"]) == len(full.content)
    etag = full.headers["etag"]
    assert etag == f'"sha256-{hashlib.sha256(full.content).hexdigest()}"'
    assert full.headers["content-disposition"].startswith("inline; filename=")

    not_modified = await client.get(url, headers={**tokens["operator"], "If-None-Match": etag, "Range": "bytes=0-3"})
    assert not_modified.status_code == 304 and not_modified.content == b""
    partial = await client.get(url, headers={**tokens["operator"], "Range": "bytes=0-9"})
    assert partial.status_code == 206 and partial.content == full.content[:10]
    assert partial.headers["content-range"] == f"bytes 0-9/{len(full.content)}"
    suffix = await client.get(url, headers={**tokens["operator"], "Range": "bytes=-5"})
    assert suffix.status_code == 206 and suffix.content == full.content[-5:]
    multi = await client.get(url, headers={**tokens["operator"], "Range": "bytes=0-1,4-5"})
    assert multi.status_code == 200 and multi.content == full.content
    stale = await client.get(url, headers={**tokens["operator"], "Range": "bytes=0-9", "If-Range": '"sha256-old"'})
    assert stale.status_code == 200
    fresh = await client.get(url, headers={**tokens["operator"], "Range": "bytes=0-9", "If-Range": etag})
    assert fresh.status_code == 206
    unsatisfiable = await client.get(url, headers={**tokens["operator"], "Range": f"bytes={len(full.content)}-"})
    assert unsatisfiable.status_code == 416 and unsatisfiable.headers["content-range"] == f"bytes */{len(full.content)}"
    assert unsatisfiable.json()["code"] == "RANGE_NOT_SATISFIABLE"
    assert (await client.get(url)).status_code == 401
    assert (await client.get(url, headers=tokens["ml"])).status_code == 403
    assert (await client.get(f"/api/v1/documents/{uuid.uuid4()}", headers=tokens["operator"])).json()["code"] == "DOCUMENT_NOT_FOUND"


async def _document(database, *, sealed: bool, uri: str, sha: str) -> str:
    async with database.session() as s, s.begin():
        row = await fetch_one(s, """
            INSERT INTO rsw.document_revisions (document_key, revision_key, source_kind, title, language, source_uri,
                                                source_sha256, extraction_version)
            VALUES (:k, 'r1', 'manual', 'test doc', 'en', :u, :h, 'x') RETURNING id""",
            k=f"test-{uuid.uuid4().hex}", u=uri, h=sha)
        if sealed:
            await execute(s, "UPDATE rsw.document_revisions SET sealed_at = now() WHERE id = :id", id=row["id"])
    return str(row["id"])


async def test_document_state_and_integrity_errors(client, tokens, database, cfg):
    pdf = make_text_pdf([["test"]])
    (cfg.STORAGE_ROOT / "documents").mkdir(parents=True, exist_ok=True)
    (cfg.STORAGE_ROOT / "documents" / "ok.pdf").write_bytes(pdf)
    (cfg.STORAGE_ROOT / "documents" / "note.txt").write_bytes(b"plain text")
    sha_pdf, sha_txt = hashlib.sha256(pdf).hexdigest(), hashlib.sha256(b"plain text").hexdigest()

    cases = [
        (await _document(database, sealed=False, uri="storage:documents/ok.pdf", sha=sha_pdf), 409, "DOCUMENT_NOT_SEALED"),
        (await _document(database, sealed=True, uri="storage:documents/note.txt", sha=sha_txt), 409, "DOCUMENT_NOT_PDF"),
        (await _document(database, sealed=True, uri="storage:documents/missing.pdf", sha=sha_pdf), 503, "DOCUMENT_STORAGE_UNAVAILABLE"),
        (await _document(database, sealed=True, uri="storage:documents/ok.pdf", sha="0" * 64), 503, "DOCUMENT_INTEGRITY_ERROR"),
        (await _document(database, sealed=True, uri="file:///etc/passwd", sha=sha_pdf), 503, "DOCUMENT_STORAGE_UNAVAILABLE"),
        (await _document(database, sealed=True, uri="storage:documents/ok.pdf", sha=sha_pdf), 200, None),
    ]
    for revision_id, status, code in cases:
        response = await client.get(f"/api/v1/documents/{revision_id}", headers=tokens["operator"])
        assert response.status_code == status, (code, response.text)
        if code:
            assert response.json()["code"] == code
            assert "/data" not in response.text and "passwd" not in response.text, "내부 경로를 노출하지 않는다"
