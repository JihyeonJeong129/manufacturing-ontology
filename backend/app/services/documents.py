"""GET /documents/{revision_id}: 봉인된 판본의 원본 PDF 제공 (API_SPEC.md 7장, RFC 9110).

- revision UUID로만 파일을 찾는다. source_uri는 설정된 STORAGE_ROOT 아래 파일만 허용한다.
- 제공 전 실제 파일의 SHA-256을 source_sha256과 비교한다. 불일치 파일은 제공하지 않는다.
- ETag는 실제 제공 파일 해시로 만든 강한 validator: "sha256-<hex>".
- If-None-Match 일치 → 304 (Range보다 우선). 단일 bytes 범위만 지원하며, 다중/해석 불가 Range는
  무시하고 200. 만족할 수 없는 단일 범위는 416. If-Range는 강한 ETag가 정확히 같을 때만 Range 적용.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from app.core.errors import ApiError

PDF_MAGIC = b"%PDF-"
_HASH_CACHE: dict[tuple[str, int, int], str] = {}


@dataclass(frozen=True)
class ByteRange:
    start: int
    end: int  # inclusive

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeNotSatisfiable(Exception):
    pass


def resolve_storage_path(source_uri: str, storage_root: Path) -> Path:
    """storage:<상대경로> 또는 STORAGE_ROOT 아래의 file:// 경로만 허용한다."""
    root = storage_root.resolve()
    if source_uri.startswith("storage:"):
        relative = source_uri[len("storage:"):].lstrip("/")
        candidate = (root / unquote(relative)).resolve()
    else:
        parsed = urlparse(source_uri)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            raise ApiError(503, "DOCUMENT_STORAGE_UNAVAILABLE", "문서 저장소에 접근할 수 없습니다.")
        candidate = Path(unquote(parsed.path)).resolve()
    if candidate != root and root not in candidate.parents:
        raise ApiError(503, "DOCUMENT_STORAGE_UNAVAILABLE", "문서 저장소에 접근할 수 없습니다.")
    return candidate


def file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _HASH_CACHE.get(key)
    if cached:
        return cached
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    value = digest.hexdigest()
    _HASH_CACHE[key] = value
    return value


def etag_for(sha256_hex: str) -> str:
    return f'"sha256-{sha256_hex}"'


def _split_etags(header: str) -> list[str]:
    return [part.strip() for part in re.findall(r'(?:W/)?"[^"]*"|\*', header)]


def if_none_match_hits(header: str | None, etag: str) -> bool:
    """If-None-Match는 약한 비교를 사용한다 (RFC 9110 13.1.2)."""
    if not header:
        return False
    for tag in _split_etags(header):
        if tag == "*" or tag.removeprefix("W/") == etag:
            return True
    return False


def if_range_allows(header: str | None, etag: str) -> bool:
    """If-Range는 강한 비교만 허용한다. 날짜/약한 validator는 Range를 무시한다."""
    if header is None:
        return True
    value = header.strip()
    return value == etag and not value.startswith("W/")


def parse_range(header: str | None, size: int) -> ByteRange | None:
    """단일 bytes 범위를 해석한다. 무시해야 하면 None, 만족 불가면 RangeNotSatisfiable."""
    if header is None:
        return None
    match = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", header)
    if match is None:
        return None  # 다중 범위, 다른 단위, 문법 오류 → 전체 파일
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0:
            raise RangeNotSatisfiable()
        if size == 0:
            raise RangeNotSatisfiable()
        return ByteRange(max(0, size - suffix), size - 1)
    start = int(first)
    end = int(last) if last != "" else size - 1
    if last != "" and end < start:
        return None  # 문법상 잘못된 범위 → 무시
    if start >= size:
        raise RangeNotSatisfiable()
    return ByteRange(start, min(end, size - 1))


def content_disposition(title: str, source_uri: str) -> str:
    name = Path(unquote(urlparse(source_uri).path or source_uri)).name or "document.pdf"
    if not name.lower().endswith(".pdf"):
        name = f"{title or 'document'}.pdf"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", ascii_name).strip() or "document.pdf"
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"


def read_bytes(path: Path, byte_range: ByteRange | None, chunk: int = 256 * 1024):
    with path.open("rb") as handle:
        if byte_range is None:
            while block := handle.read(chunk):
                yield block
            return
        handle.seek(byte_range.start)
        remaining = byte_range.length
        while remaining > 0:
            block = handle.read(min(chunk, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block
