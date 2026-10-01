from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_cfg, Principal, require_user
from app.api.v1.endpoints.openapi_docs import responses_doc
from app.core.config import Settings
from app.core.errors import ApiError, problem_response
from app.db.session import get_db
from app.db.sqlutil import fetch_one
from app.services import documents as doc

router = APIRouter(prefix="/documents", tags=["Documents"])

CACHE = "private, no-cache"


@router.get(
    "/{revision_id}",
    summary="봉인된 판본의 등록 원본 PDF 제공",
    response_class=Response,
    responses={
        200: {"content": {"application/pdf": {}}, "description": "PDF 전체 바이트"},
        206: {"content": {"application/pdf": {}}, "description": "만족 가능한 단일 byte 범위"},
        304: {"description": "ETag 일치; body 없음"},
        **responses_doc({}, 401, 403, 404, 409, 416, 422, 503),
    },
)
async def get_document(
    revision_id: UUID,
    request: Request,
    principal: Principal = Depends(require_user),
    db: AsyncSession = Depends(get_db),
    cfg: Settings = Depends(get_cfg),
):
    row = await fetch_one(
        db,
        "SELECT id, title, source_uri, source_sha256, sealed_at FROM rsw.document_revisions WHERE id = :id",
        id=revision_id,
    )
    await db.rollback()
    if row is None:
        raise ApiError(404, "DOCUMENT_NOT_FOUND", "문서 판본을 찾을 수 없습니다.")
    if row["sealed_at"] is None:
        raise ApiError(409, "DOCUMENT_NOT_SEALED", "아직 봉인되지 않은 문서 판본입니다.")
    path = doc.resolve_storage_path(row["source_uri"], cfg.STORAGE_ROOT)
    if not path.is_file():
        raise ApiError(503, "DOCUMENT_STORAGE_UNAVAILABLE", "원본 파일을 저장소에서 찾을 수 없습니다.")
    actual = doc.file_sha256(path)
    if actual != row["source_sha256"]:
        raise ApiError(503, "DOCUMENT_INTEGRITY_ERROR", "저장된 원본 파일이 등록된 해시와 일치하지 않습니다.")
    with path.open("rb") as handle:
        if handle.read(len(doc.PDF_MAGIC)) != doc.PDF_MAGIC:
            raise ApiError(409, "DOCUMENT_NOT_PDF", "이 판본의 원본 파일은 PDF가 아닙니다.")

    size = path.stat().st_size
    etag = doc.etag_for(actual)
    base = {"ETag": etag, "Cache-Control": CACHE, "Accept-Ranges": "bytes"}
    if doc.if_none_match_hits(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": CACHE})

    byte_range = None
    if doc.if_range_allows(request.headers.get("if-range"), etag):
        try:
            byte_range = doc.parse_range(request.headers.get("range"), size)
        except doc.RangeNotSatisfiable:
            return problem_response(
                request, 416, "RANGE_NOT_SATISFIABLE", "요청한 byte 범위를 제공할 수 없습니다.",
                headers={"Content-Range": f"bytes */{size}", "Cache-Control": CACHE},
            )
    headers = {**base, "Content-Disposition": doc.content_disposition(row["title"], row["source_uri"])}
    if byte_range is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(doc.read_bytes(path, None), status_code=200, media_type="application/pdf", headers=headers)
    headers["Content-Length"] = str(byte_range.length)
    headers["Content-Range"] = f"bytes {byte_range.start}-{byte_range.end}/{size}"
    return StreamingResponse(doc.read_bytes(path, byte_range), status_code=206, media_type="application/pdf", headers=headers)
