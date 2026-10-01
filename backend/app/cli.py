"""운영/개발 CLI.

  python -m app.cli migrate
  python -m app.cli seed-demo
  python -m app.cli release approve  --version V --reviewer demo:reviewer
  python -m app.cli release freeze   --version V
  python -m app.cli release publish  --version V
  python -m app.cli release activate --version V --actor demo:developer [--deployment pilot]
  python -m app.cli release validate --version V   (발행 없이 RDF 빌드 + SHACL만 실행)
  python -m app.cli token create --actor KEY --label L [--producer demo-ml]
  python -m app.cli sample-alert [--event-id ID] [--cycle-state unknown]

  RSW MVP 실제 지식 (RAW_DATA_DIR의 정의서·매뉴얼 5종·RAG 활용정리):
  python -m app.cli embedding-download            (이미지 빌드 단계: 임베딩 모델 고정 리비전 다운로드)
  python -m app.cli load-mvp [--no-embeddings]    (10개 상황 적재 → 검사 → 발행 → pilot 활성화)
  python -m app.cli mvp-report                    (상황별 적재 결과 표)
  python -m app.cli mvp-alert --case S01          (상황별 ML 경보 JSON, NL-S09는 자연어 시연용)
  python -m app.cli mvp-demo [--base-url URL]     (ML 오류 코드 / 자연어 질의 시연을 API로 실행)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

from app.core.config import settings
from app.core.jsonutil import dumps


async def _with_session(fn):
    from app.db.session import Database

    database = Database.from_settings(settings, null_pool=True)
    try:
        async with database.session() as session, session.begin():
            return await fn(session)
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("seed-demo")
    release = sub.add_parser("release")
    release.add_argument("action", choices=["approve", "freeze", "publish", "activate", "validate"])
    release.add_argument("--version", required=True)
    release.add_argument("--reviewer")
    release.add_argument("--actor")
    release.add_argument("--deployment", default=settings.KNOWLEDGE_DEPLOYMENT)
    token = sub.add_parser("token")
    token.add_argument("action", choices=["create"])
    token.add_argument("--actor", required=True)
    token.add_argument("--label", required=True)
    token.add_argument("--producer")
    sample = sub.add_parser("sample-alert")
    sample.add_argument("--event-id")
    sample.add_argument("--cycle-state", default="unknown", choices=["welding", "non_welding", "unknown"])
    sample.add_argument("--asset-key", default="RSW-DEMO-001")
    download = sub.add_parser("embedding-download")
    download.add_argument("--dir", default=str(settings.EMBEDDING_MODEL_DIR))
    load = sub.add_parser("load-mvp")
    load.add_argument("--raw-dir", default=str(settings.RAW_DATA_DIR))
    load.add_argument("--no-embeddings", action="store_true")
    load.add_argument("--no-activate", action="store_true")
    sub.add_parser("mvp-report")
    mvp_alert = sub.add_parser("mvp-alert")
    mvp_alert.add_argument("--case", default="S01")
    mvp_alert.add_argument("--event-id")
    demo = sub.add_parser("mvp-demo")
    demo.add_argument("--base-url", default="http://localhost:8000")
    demo.add_argument("--case", action="append")
    args = parser.parse_args(argv)

    if args.command == "embedding-download":
        from pathlib import Path

        from app.services.answering.embedding import LOCAL_SPEC, download_model

        path = download_model(Path(args.dir))
        print(f"{LOCAL_SPEC.model_name}@{LOCAL_SPEC.revision} -> {path}")
        return 0

    if args.command == "mvp-alert":
        from app.mvp.scenarios import BY_KEY, alert_payload

        if args.case not in BY_KEY:
            raise SystemExit(f"case는 {', '.join(BY_KEY)} 중 하나")
        print(json.dumps(alert_payload(BY_KEY[args.case], event_id=args.event_id), ensure_ascii=False, indent=2))
        return 0

    if args.command in ("load-mvp", "mvp-report"):
        from pathlib import Path

        from app.mvp import report as mvp_report

        async def run(session):
            result = None
            if args.command == "load-mvp":
                from app.mvp.loader import load_mvp

                result = await load_mvp(session, settings, raw_dir=Path(args.raw_dir), embeddings=not args.no_embeddings,
                                        activate=not args.no_activate)
                release_id = result["release_id"]
            else:
                release_id = await mvp_report.active_release_id(session, settings.KNOWLEDGE_DEPLOYMENT)
                if release_id is None:
                    raise SystemExit("활성 지식 배포본이 없습니다. load-mvp를 먼저 실행하세요.")
            return result, await mvp_report.release_totals(session, release_id), await mvp_report.scenario_report(session, release_id)

        result, totals, rows = asyncio.run(_with_session(run))
        if result is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        print(json.dumps(totals, ensure_ascii=False, indent=2))
        print(mvp_report.format_table(rows))
        if result is not None:
            print("\nRSW 토큰 (.env의 MVP_*_TOKEN):")
            print(f"  ML 생산자(rsw-ml): {settings.MVP_ML_TOKEN}")
            print(f"  작업자:            {settings.MVP_OPERATOR_TOKEN}")
        return 0

    if args.command == "mvp-demo":
        from app.db.sqlutil import fetch_all
        from app.mvp.demo import describe, run_case
        from app.mvp.scenarios import DEMO_ML_CASE, DEMO_NL_CASE

        for case_key in args.case or [DEMO_ML_CASE, DEMO_NL_CASE]:
            result = run_case(args.base_url.rstrip("/"), settings.MVP_ML_TOKEN, settings.MVP_OPERATOR_TOKEN, case_key)

            async def candidates(session, answer_id=result["answer"]["answer_id"]):
                return await fetch_all(session, """
                    SELECT rc.method, rc.rank, rc.score, ds.section_key
                    FROM rsw.retrieval_candidates rc
                    JOIN rsw.retrieval_chunks c ON c.id = rc.chunk_id
                    JOIN rsw.document_sections ds ON ds.id = c.section_id
                    WHERE rc.run_id = :id ORDER BY rc.method, rc.rank""", id=answer_id)

            print(describe(result, asyncio.run(_with_session(candidates))))
            print()
        return 0

    if args.command == "migrate":
        from app.db.migrate import upgrade_head

        upgrade_head()
        print("migrations applied")
        return 0

    if args.command == "sample-alert":
        from app.demo.seed import sample_alert

        print(json.dumps(sample_alert(args.event_id, cycle_state=args.cycle_state, asset_key=args.asset_key), ensure_ascii=False, indent=2))
        return 0

    if args.command == "seed-demo":
        from app.demo.seed import seed_demo

        result = asyncio.run(_with_session(lambda s: seed_demo(s, settings)))
        report = result.pop("report", None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if report:
            print(f"SHACL conforms={report['shacl']['conforms']} triples={report['graph']['triples']} "
                  f"graph={report['graph']['uri']}")
        print("\n데모 토큰 (.env의 DEMO_*_TOKEN):")
        print(f"  ML 생산자: {settings.DEMO_ML_TOKEN}")
        print(f"  작업자:    {settings.DEMO_OPERATOR_TOKEN}")
        print(f"  검토자:    {settings.DEMO_REVIEWER_TOKEN}")
        return 0

    if args.command == "token":
        from app.api.deps import token_sha256
        from app.db.sqlutil import execute, fetch_one

        value = secrets.token_urlsafe(32)

        async def create(session):
            actor = await fetch_one(session, "SELECT id FROM rsw.actors WHERE external_key = :k", k=args.actor)
            if actor is None:
                raise SystemExit(f"actor {args.actor} not found")
            producer_id = None
            if args.producer:
                producer = await fetch_one(session, "SELECT id FROM rsw.event_producers WHERE producer_key = :k", k=args.producer)
                if producer is None:
                    raise SystemExit(f"producer {args.producer} not found")
                producer_id = producer["id"]
            await execute(session, "INSERT INTO rsw.api_credentials (actor_id, token_sha256, producer_id, label) VALUES (:a, :t, :p, :l)",
                          a=actor["id"], t=token_sha256(value), p=producer_id, l=args.label)

        asyncio.run(_with_session(create))
        print(value)
        return 0

    from app.ontology import publisher

    async def release_action(session):
        rel = await publisher.release_by_version(session, args.version)
        if args.action == "approve":
            await publisher.approve(session, rel["id"], await publisher.actor_id(session, args.reviewer))
            return {"approved": args.version}
        if args.action == "freeze":
            await publisher.freeze(session, rel["id"])
            return {"frozen": args.version}
        if args.action == "publish":
            report = await publisher.publish(session, rel["id"], settings.STORAGE_ROOT)
            return {k: report[k] for k in ("result", "graph", "shacl", "domain_checks", "contract")}
        if args.action == "activate":
            await publisher.activate(session, rel["id"], args.deployment, await publisher.actor_id(session, args.actor))
            return {"activated": args.version, "deployment": args.deployment}
        from app.ontology import builder

        snapshot = await builder.load_snapshot(session, rel["id"])
        conforms, text, violations = publisher.validate_graph(builder.build_graph(snapshot))
        return {"conforms": conforms, "violations": violations, "report": text}

    result = asyncio.run(_with_session(release_action))
    print(dumps(result) if not isinstance(result, dict) else json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
