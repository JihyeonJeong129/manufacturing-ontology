"""Dependency-free structural checks. This is NOT a PostgreSQL syntax parser.

Checks table/column declarations and FK targets; does not execute SQL or PL/pgSQL.
"""
from pathlib import Path
import re


def split_sql_items(text):
    items, start, depth, quoted = [], 0, 0, False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "'":
            if quoted and i + 1 < len(text) and text[i + 1] == "'":
                i += 2
                continue
            quoted = not quoted
        elif not quoted:
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            elif c == ',' and depth == 0:
                items.append(text[start:i].strip())
                start = i + 1
        i += 1
    items.append(text[start:].strip())
    return items


def read_tables(sql):
    tables = {}
    for match in re.finditer(r'CREATE TABLE (\w+)\s*\(', sql):
        start, depth, quoted, i = match.end(), 1, False, match.end()
        while depth:
            c = sql[i]
            if c == "'":
                if quoted and i + 1 < len(sql) and sql[i + 1] == "'":
                    i += 2
                    continue
                quoted = not quoted
            elif not quoted:
                depth += (c == '(') - (c == ')')
            i += 1
        definitions = split_sql_items(sql[start:i-1])
        columns, constraints, unique_keys, fks = {}, [], [], []
        for definition in definitions:
            first = definition.split()[0]
            if first in {'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT'}:
                constraints.append(definition)
                unique = re.match(r'(?:PRIMARY KEY|UNIQUE)\s*\(([^)]+)\)', definition)
                if unique:
                    unique_keys.append(tuple(c.strip() for c in unique[1].split(',')))
                fk = re.match(r'FOREIGN KEY\s*\(([^)]+)\)\s*REFERENCES\s+(\w+)\s*\(([^)]+)\)', definition)
                if fk:
                    fks.append((tuple(c.strip() for c in fk[1].split(',')), fk[2], tuple(c.strip() for c in fk[3].split(','))))
            else:
                if first in columns:
                    raise AssertionError(f'Duplicate column: {match[1]}.{first}')
                columns[first] = definition[len(first):].strip()
                if re.search(r'\b(PRIMARY KEY|UNIQUE)\b', definition):
                    unique_keys.append((first,))
                fk = re.search(r'\bREFERENCES\s+(\w+)\s*\(([^)]+)\)', definition)
                if fk:
                    fks.append(((first,), fk[1], tuple(c.strip() for c in fk[2].split(','))))
        if match[1] in tables:
            raise AssertionError(f'Duplicate table: {match[1]}')
        tables[match[1]] = dict(columns=columns, constraints=constraints, unique_keys=unique_keys, fks=fks)
    return tables


def validate(path):
    sql = path.read_text()
    tables = read_tables(sql)
    errors = []
    fk_count = 0
    for name, table in tables.items():
        if not any('PRIMARY KEY' in d for d in list(table['columns'].values()) + table['constraints']):
            errors.append(f'{name}: no primary key')
        for cols, target, refs in table['fks']:
            fk_count += 1
            if target not in tables:
                errors.append(f'{name}: missing FK target {target}')
                continue
            if len(cols) != len(refs):
                errors.append(f'{name}: FK arity mismatch')
            if any(c not in table['columns'] for c in cols):
                errors.append(f'{name}: missing local FK column {cols}')
            if any(c not in tables[target]['columns'] for c in refs):
                errors.append(f'{name}: missing referenced FK column {target}{refs}')
            elif refs not in tables[target]['unique_keys']:
                errors.append(f'{name}: target {target}{refs} is not a declared unique key')
            else:
                for c, r in zip(cols, refs):
                    local_type = table['columns'][c].split()[0]
                    target_type = tables[target]['columns'][r].split()[0]
                    if local_type != target_type:
                        errors.append(f'{name}.{c}: type {local_type} differs from {target}.{r}: {target_type}')
    for table_name in re.findall(r"'([a-z_]+)'", sql[sql.index('DO $$'):]):
        if table_name not in tables:
            errors.append(f'Trigger registration names missing table: {table_name}')
    if 'CREATE INDEX' in sql and re.search(r'\bUSING\s+(hnsw|ivfflat)\b', sql, re.I):
        errors.append('v1 unexpectedly contains an ANN index')
    if errors:
        raise AssertionError('\n'.join(errors))
    return tables, fk_count


if __name__ == '__main__':
    tables, fk_count = validate(Path(__file__).with_name('schema.sql'))
    print(f'PASS: {len(tables)} tables; {sum(len(t["columns"]) for t in tables.values())} columns; {fk_count} foreign keys')
    print('Scope: declarations, FK existence/types/unique targets, PKs, trigger table names, no ANN index.')
    print('NOT tested: PostgreSQL grammar, extension availability, trigger execution, concurrency, query plans.')
