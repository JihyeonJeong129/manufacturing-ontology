from pathlib import Path

import pytest

from app.core.errors import ApiError
from app.demo.pdf import make_text_pdf
from app.services import documents as d

ETAG = d.etag_for("a" * 64)


@pytest.mark.parametrize("header, expected", [
    ("bytes=0-9", (0, 9)),
    ("bytes=10-", (10, 99)),
    ("bytes=-10", (90, 99)),
    ("bytes=-1000", (0, 99)),
    ("bytes=90-500", (90, 99)),
    (" bytes = 5 - 6 ", (5, 6)),
])
def test_single_ranges(header, expected):
    r = d.parse_range(header, 100)
    assert (r.start, r.end) == expected and r.length == expected[1] - expected[0] + 1


@pytest.mark.parametrize("header", [None, "bytes=0-1,5-6", "items=0-1", "bytes=9-1", "bytes=-", "garbage"])
def test_ignored_ranges_return_none(header):
    assert d.parse_range(header, 100) is None


@pytest.mark.parametrize("header, size", [("bytes=100-", 100), ("bytes=-0", 100), ("bytes=0-", 0)])
def test_unsatisfiable_ranges(header, size):
    with pytest.raises(d.RangeNotSatisfiable):
        d.parse_range(header, size)


def test_conditional_headers():
    assert d.if_none_match_hits(ETAG, ETAG)
    assert d.if_none_match_hits(f'"other", W/{ETAG}', ETAG)  # If-None-Match는 약한 비교
    assert d.if_none_match_hits("*", ETAG)
    assert not d.if_none_match_hits('"other"', ETAG)
    assert d.if_range_allows(None, ETAG)
    assert d.if_range_allows(ETAG, ETAG)
    assert not d.if_range_allows(f"W/{ETAG}", ETAG)            # If-Range는 강한 비교만
    assert not d.if_range_allows("Wed, 21 Oct 2015 07:28:00 GMT", ETAG)


def test_storage_paths_cannot_escape_root(tmp_path: Path):
    (tmp_path / "documents").mkdir()
    assert d.resolve_storage_path("storage:documents/a.pdf", tmp_path) == (tmp_path / "documents/a.pdf").resolve()
    assert d.resolve_storage_path(f"file://{tmp_path}/documents/a.pdf", tmp_path) == (tmp_path / "documents/a.pdf").resolve()
    for uri in ("storage:../etc/passwd", "storage:documents/%2e%2e/%2e%2e/x", "file:///etc/passwd",
                "https://example.com/a.pdf", "s3://bucket/a.pdf", "file://remote-host/a.pdf"):
        with pytest.raises(ApiError) as info:
            d.resolve_storage_path(uri, tmp_path)
        assert info.value.code == "DOCUMENT_STORAGE_UNAVAILABLE"


def test_content_disposition_is_header_safe():
    header = d.content_disposition("매뉴얼", "storage:documents/매뉴얼 \"v1\".pdf")
    assert header.startswith('inline; filename="')
    assert "\n" not in header and "filename*=UTF-8''" in header
    assert '"v1"' not in header.split(";")[1]


def test_read_bytes_ranges(tmp_path: Path):
    path = tmp_path / "f.bin"
    path.write_bytes(bytes(range(256)) * 10)
    assert b"".join(d.read_bytes(path, d.ByteRange(10, 19), chunk=3)) == path.read_bytes()[10:20]
    assert b"".join(d.read_bytes(path, None, chunk=100)) == path.read_bytes()


def test_generated_pdf_structure():
    data = make_text_pdf([["Page (1) \\ text"], ["Page 2"]])
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref_offset = int(data.rsplit(b"startxref", 1)[1].split()[0])
    assert data[xref_offset:].startswith(b"xref")
    entries = data[xref_offset:].split(b"\n")[3:3 + 6]
    for number, entry in enumerate(entries, start=1):
        offset = int(entry.split()[0])
        assert data[offset:].startswith(f"{number} 0 obj".encode())
    assert b"\\(1\\)" in data and b"\\\\" in data
    with pytest.raises(UnicodeEncodeError):
        make_text_pdf([["한글"]])
