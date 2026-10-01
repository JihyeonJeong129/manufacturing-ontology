"""SQL 스크립트를 문장 단위로 나눈다.

asyncpg는 준비된 문장(prepared statement)에 여러 문장을 넣을 수 없으므로 마이그레이션은
스크립트를 문장으로 나눠 순서대로 실행한다. 작은따옴표 문자열, 큰따옴표 식별자,
$tag$ 달러 인용, -- 및 /* */ 주석 안의 세미콜론은 구분자로 취급하지 않는다.
"""
from __future__ import annotations

import re
from pathlib import Path

_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_sql(script: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    i, n = 0, len(script)
    while i < n:
        c = script[i]
        nxt = script[i + 1] if i + 1 < n else ""
        if c == "-" and nxt == "-":
            end = script.find("\n", i)
            end = n if end == -1 else end
            i = end
            continue
        if c == "/" and nxt == "*":
            depth, j = 1, i + 2
            while j < n and depth:
                if script.startswith("/*", j):
                    depth += 1
                    j += 2
                elif script.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise ValueError("Unterminated block comment")
            buf.append(" ")
            i = j
            continue
        if c in ("'", '"'):
            j = i + 1
            while True:
                if j >= n:
                    raise ValueError(f"Unterminated quoted text starting at offset {i}")
                if script[j] == c:
                    if j + 1 < n and script[j + 1] == c:
                        j += 2
                        continue
                    break
                j += 1
            buf.append(script[i : j + 1])
            i = j + 1
            continue
        if c == "$":
            m = _DOLLAR_TAG.match(script, i)
            # $1 같은 위치 매개변수는 태그가 아니다.
            if m and not (i > 0 and (script[i - 1].isalnum() or script[i - 1] == "_")):
                tag = m.group(0)
                end = script.find(tag, m.end())
                if end == -1:
                    raise ValueError(f"Unterminated dollar-quoted text {tag}")
                buf.append(script[i : end + len(tag)])
                i = end + len(tag)
                continue
        if c == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


SQL_DIR = Path(__file__).with_name("sql")


def load_statements(name: str) -> list[str]:
    return split_sql((SQL_DIR / name).read_text(encoding="utf-8"))
