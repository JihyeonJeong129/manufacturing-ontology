"""docs/design/api/openapi.json(설계 계약)으로 실제 요청/응답을 검증하는 도우미."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

FIXTURES = Path(__file__).parent / "fixtures"
OPENAPI_URI = "urn:rsw:openapi"


@lru_cache
def openapi() -> dict[str, Any]:
    return json.loads((FIXTURES / "openapi.json").read_text(encoding="utf-8"))


@lru_cache
def examples() -> dict[str, Any]:
    return json.loads((FIXTURES / "examples.json").read_text(encoding="utf-8"))


@lru_cache
def _registry() -> Registry:
    return Registry().with_resource(OPENAPI_URI, Resource.from_contents(openapi(), default_specification=DRAFT202012))


@lru_cache
def validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator({"$ref": f"{OPENAPI_URI}#/components/schemas/{schema_name}"}, registry=_registry())


def contract_errors(instance: Any, schema_name: str) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path))}: {e.message}" for e in validator(schema_name).iter_errors(instance)]


def assert_contract(instance: Any, schema_name: str) -> None:
    errors = contract_errors(instance, schema_name)
    assert not errors, f"{schema_name} contract violations: {errors}"
