from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.core.jsonutil import JsonContentError, JsonParseError, as_json, dumps, strict_loads, utc_iso


@pytest.mark.parametrize("raw", [
    b'{"a": 1, "a": 2}',
    b'{"a": NaN}',
    b'{"a": Infinity}',
    b'{"a": -Infinity}',
    b'{"a": 1e400}',
    b'{"a": ',
    b"\xff\xfe{}",
    "﻿{}".encode("utf-8"),
])
def test_strict_loads_rejects_as_parse_error(raw):
    with pytest.raises(JsonParseError):
        strict_loads(raw)


def test_duplicate_keys_in_nested_objects_are_rejected():
    with pytest.raises(JsonParseError):
        strict_loads(b'{"outer": {"k": 1, "k": 1}}')


@pytest.mark.parametrize("raw, pointer", [
    (b'{"a": "x\\u0000y"}', "/a"),
    (b'{"list": ["ok", "\\ud800"]}', "/list/1"),
    (b'{"k\\u0000": 1}', "/k\x00"),
])
def test_strings_postgres_cannot_store_are_content_errors(raw, pointer):
    with pytest.raises(JsonContentError) as info:
        strict_loads(raw)
    assert info.value.pointer == pointer


def test_valid_json_keeps_submitted_structure():
    value = strict_loads('{"b": [1, 2.5, true, null], "a": {"한글": "값"}}'.encode())
    assert value == {"b": [1, 2.5, True, None], "a": {"한글": "값"}}


def test_utc_iso_normalizes_offsets_and_naive_values():
    kst = timezone(timedelta(hours=9))
    assert utc_iso(datetime(2026, 9, 26, 10, 0, tzinfo=kst)) == "2026-09-26T01:00:00Z"
    assert utc_iso(datetime(2026, 9, 26, 1, 0)) == "2026-09-26T01:00:00Z"
    assert utc_iso(None) is None


def test_dumps_is_deterministic_and_rejects_nan():
    uid = UUID("00000000-0000-4000-8000-000000000001")
    assert dumps({"id": uid, "t": datetime(2026, 1, 1, tzinfo=timezone.utc)}) == \
        '{"id":"00000000-0000-4000-8000-000000000001","t":"2026-01-01T00:00:00Z"}'
    with pytest.raises(ValueError):
        dumps({"x": float("nan")})


def test_as_json_decodes_driver_strings():
    assert as_json('{"a": 1}') == {"a": 1}
    assert as_json(b"[1]") == [1]
    assert as_json({"a": 1}) == {"a": 1}
    assert as_json(None) is None
