from app.db.sql_script import load_statements, split_sql


def test_splits_simple_statements_and_skips_comments():
    sql = "-- header; not a statement\nCREATE TABLE a (x int);\n/* block; comment */ INSERT INTO a VALUES (1);"
    assert split_sql(sql) == ["CREATE TABLE a (x int)", "INSERT INTO a VALUES (1)"]


def test_semicolons_inside_quotes_and_dollar_bodies_are_kept():
    sql = (
        "INSERT INTO t VALUES ('a;b', 'it''s; fine');\n"
        'SELECT "odd;name" FROM t;\n'
        "CREATE FUNCTION f() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'x;y'; END; $$;\n"
        "DO $body$ BEGIN PERFORM 1; END $body$;"
    )
    statements = split_sql(sql)
    assert len(statements) == 4
    assert statements[2].endswith("END; $$")
    assert statements[3].startswith("DO $body$")


def test_positional_parameters_are_not_dollar_quotes():
    assert split_sql("SELECT $1, $2; SELECT 2") == ["SELECT $1, $2", "SELECT 2"]


def test_nested_block_comment():
    assert split_sql("/* a /* b; */ c; */ SELECT 1;") == ["SELECT 1"]


def test_schema_file_contains_all_43_tables_and_triggers():
    statements = load_statements("rsw_schema.sql")
    tables = [s for s in statements if s.startswith("CREATE TABLE")]
    assert len(tables) == 43
    assert not any(s.strip().upper() in ("BEGIN", "COMMIT") for s in statements)
    assert statements[0].startswith("CREATE EXTENSION IF NOT EXISTS vector")
    assert any(s.startswith("DO $$") for s in statements)
    # 설계 원본에 없는 ANN 인덱스를 추가하지 않았는지 확인 (MVP는 exact search)
    assert not any("USING hnsw" in s or "USING ivfflat" in s for s in statements)


def test_api_support_migration_tables():
    statements = load_statements("api_support.sql")
    created = {s.split()[2] for s in statements if s.startswith("CREATE TABLE")}
    assert created == {"background_jobs", "api_credentials", "api_idempotency_requests"}
    assert any("feedback_one_successor" in s and "WHERE supersedes_id IS NOT NULL" in s for s in statements)
