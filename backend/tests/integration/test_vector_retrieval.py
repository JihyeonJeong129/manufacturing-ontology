"""임베딩 모델을 설정하면 run에 고정된 모델의 벡터로만 검색한다 (pgvector exact search)."""
import hashlib
import uuid

import httpx

from app.db.sqlutil import execute, fetch_all, fetch_one
from app.main import create_app
from app.services.answering.embedding import register_embedder, unregister_embedder
from tests.integration.helpers import ask_and_answer, ready_alert


class FakeEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


async def test_vector_candidates_use_the_runs_embedding_model(cfg, database, tokens, worker, seed):
    async with database.session() as s, s.begin():
        model = await fetch_one(s, """
            INSERT INTO rsw.embedding_models (provider, model_name, model_version, dimensions, distance_metric,
                                              preprocessing_version, config_sha256)
            VALUES ('test', :name, 'v1', 3, 'cosine', 'v1', :sha) RETURNING id""",
            name=f"fake-{uuid.uuid4().hex[:6]}", sha=hashlib.sha256(b"fake").hexdigest())
        chunks = await fetch_all(s, "SELECT chunk_id, section_id FROM rsw.release_chunks WHERE release_id = :r ORDER BY chunk_id",
                                 r=seed["release_id"])
        target = next(c for c in chunks if str(c["section_id"]) == seed["entities"]["sec-3.2"])
        for c in chunks:
            vector = "[1,0,0]" if c is target else "[0,1,0]"
            await execute(s, "INSERT INTO rsw.chunk_embeddings (chunk_id, embedding_model_id, dimensions, embedding) VALUES (:c, :m, 3, CAST(:v AS public.vector))",
                          c=c["chunk_id"], m=model["id"], v=vector)
    register_embedder(model["id"], FakeEmbedder())
    try:
        app = create_app(cfg.model_copy(update={"EMBEDDING_MODEL_ID": model["id"]}), database)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            alert = await ready_alert(c, tokens, worker)
            done = await ask_and_answer(c, tokens, worker, alert, question="의미 기반 질문")
    finally:
        unregister_embedder(model["id"])
    async with database.session() as s:
        vector_rows = await fetch_all(s, """SELECT chunk_id, rank, score FROM rsw.retrieval_candidates
                                            WHERE run_id = :r AND method = 'vector' ORDER BY rank""", r=done["answer_id"])
        run = await fetch_one(s, "SELECT embedding_model_id FROM rsw.assistant_runs WHERE id = :r", r=done["answer_id"])
    assert run["embedding_model_id"] == model["id"]
    assert vector_rows and vector_rows[0]["chunk_id"] == target["chunk_id"] and vector_rows[0]["score"] == 0.0
