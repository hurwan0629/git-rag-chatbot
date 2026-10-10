"""GitHub repo 수집·청킹·임베딩·저장 및 단일 HTML 생성 CLI."""

import argparse
import importlib.metadata
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'repo_ingestion'

from .config import (CHUNKING, DEFAULT_EXTENSIONS, DIMENSION, MODEL_ID, OUTPUT_DIR, PREFIX, extensions,
                     filter_settings, load_env, parse_repo_url, positive_int, safe_error, validate_path)
from .storage import save_dataset, validate_dataset, write_json


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     epilog='고정 설정: E5 small · 최종 입력 512 tokens · overlap 0 · output/index.html')
    parser.add_argument('repo_url', metavar='REPO_URL', help='GitHub HTTPS repo 루트 URL')
    parser.add_argument('--ref', help='branch/tag/commit; 생략하면 기본 브랜치')
    parser.add_argument('--scope', default='', metavar='PATH', help='repo 내부 대상 폴더')
    parser.add_argument('--env-file', metavar='PATH', help='사용할 .env; 생략하면 프로젝트 루트부터 상위 탐색')
    parser.add_argument('--collect-only', action='store_true', help='encoder 없이 원문·청크·HTML만 저장')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto', help='추론 장치')
    parser.add_argument('--batch-size', type=positive_int, default=8, metavar='N', help='임베딩 배치 크기')
    parser.add_argument('--max-file-bytes', type=positive_int, default=1000000, metavar='N', help='파일별 최대 bytes')
    parser.add_argument('--extensions', type=extensions, default=sorted(DEFAULT_EXTENSIONS),
                        metavar='LIST', help='허용 확장자를 쉼표로 지정; 기본 코드·설정 파일 목록을 대체')
    return parser


def initial_manifest(repo, args, token):
    libraries = {'python': sys.version.split()[0]}
    for name in ('numpy', 'python-dotenv', 'torch', 'transformers', 'huggingface-hub'):
        try:
            libraries[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            libraries[name] = 'not installed'
    return dict(schema_version='1.0', run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_') + uuid.uuid4().hex,
                repo=repo, repo_url='https://github.com/' + repo, ref_requested=args.ref,
                commit_sha=None, tree_sha=None, scope_path=args.scope,
                mode='collect_only' if args.collect_only else 'embed', status='running',
                started_at=utc_now(), finished_at=None, inventory_complete=False, auth_used=bool(token),
                directories=[''], filter=filter_settings(args), chunking=dict(CHUNKING),
                embedding=dict(model_id=MODEL_ID, revision=None, prefix=PREFIX, pooling='masked_mean', normalize=True,
                               dimension=DIMENSION, dtype='float32', batch_size=args.batch_size, device=None,
                               encoder_loaded=False),
                counts={key: 0 for key in ('listed_files', 'collected_files', 'excluded_files', 'failed_files',
                                          'documents', 'chunks', 'vectors')},
                timings_sec={key: 0.0 for key in ('listing', 'collecting', 'chunking', 'embedding', 'saving', 'reporting', 'total')},
                libraries=libraries, artifacts=dict(files='files.csv', documents='documents.jsonl', chunks='chunks.jsonl',
                                                   vectors=None, raw_dir='raw'), report_status='pending', errors=[])


def execute(args, repo, token, output=OUTPUT_DIR):
    from .chunker import chunk_document
    from .collector import collect
    from .embedder import embed_chunks, load_encoder, load_tokenizer, resolve_revision
    from .github_client import GitHubClient
    from .report import build_report

    manifest = initial_manifest(repo, args, token)
    folder = Path(output) / repo.replace('/', '--') / manifest['run_id']
    folder.mkdir(parents=True, exist_ok=False)
    write_json(folder / 'manifest.json', manifest)
    files, documents, chunks, vectors, tokenizer = [], [], [], None, None
    stage, started, stage_started = 'listing', time.perf_counter(), time.perf_counter()
    exit_code = 0
    try:
        print('[1/5] commit 확정 및 파일 목록 조회', flush=True)
        client = GitHubClient(repo, token)
        commit, tree_sha = client.snapshot(args.ref)
        manifest.update(commit_sha=commit, tree_sha=tree_sha)
        entries, complete, errors = client.inventory(tree_sha)
        manifest['inventory_complete'] = complete
        manifest['errors'].extend(errors)
        directories = []
        for entry in entries:
            if entry.get('type') == 'tree':
                directories.append(validate_path(entry['path']))
        manifest['directories'] = ['', *sorted(set(directories))]
        manifest['timings_sec'][stage] = time.perf_counter() - stage_started
        stage, stage_started = 'collecting', time.perf_counter()
        if args.scope and complete and args.scope not in directories:
            exit_code = 2
            raise ValueError('--scope에 해당하는 repo 내부 폴더가 없습니다.')
        print('[2/5] 텍스트 파일 수집·검증', flush=True)
        files, documents = collect(client, entries, repo, commit, manifest['run_id'], args.scope, manifest['filter'])
        manifest['timings_sec'][stage] = time.perf_counter() - stage_started
        stage, stage_started = 'chunking', time.perf_counter()
        print('[3/5] 모델 revision·tokenizer 준비 및 청킹', flush=True)
        revision = resolve_revision()
        manifest['embedding']['revision'] = revision
        tokenizer = load_tokenizer(revision)
        for document in documents:
            try:
                chunks.extend(chunk_document(document, tokenizer, revision))
            except Exception as error:
                manifest['errors'].append(dict(stage=stage, path=document['path'], code='chunking_failed',
                                               message=safe_error(error, token)))
        manifest['timings_sec'][stage] = time.perf_counter() - stage_started
        stage, stage_started = 'embedding', time.perf_counter()
        if args.collect_only:
            print('[4/5] collect-only: encoder·벡터 생성 생략', flush=True)
        else:
            import numpy as np
            print('[4/5] encoder 로딩·벡터 생성', flush=True)
            if chunks:
                model, device = load_encoder(revision, args.device)
                manifest['embedding'].update(device=device, encoder_loaded=True)
                vectors, errors = embed_chunks(chunks, tokenizer, model, device, args.batch_size)
                manifest['errors'].extend(errors)
            else:
                vectors = np.empty((0, DIMENSION), dtype=np.float32)
            manifest['artifacts']['vectors'] = 'vectors.npy'
        manifest['timings_sec'][stage] = time.perf_counter() - stage_started
    except Exception as error:
        manifest['timings_sec'][stage] = time.perf_counter() - stage_started
        manifest['errors'].append(dict(stage=stage, path=None, code=getattr(error, 'code', stage + '_failed'),
                                       message=safe_error(error, token)))
        if stage == 'embedding':
            for chunk in chunks:
                if chunk['embedding_status'] == 'not_run':
                    chunk.update(embedding_status='failed', embedding_error=safe_error(error, token))

    chunk_counts, vector_counts = {}, {}
    for chunk in chunks:
        identifier = chunk['document_id']
        chunk_counts[identifier] = chunk_counts.get(identifier, 0) + 1
        vector_counts[identifier] = vector_counts.get(identifier, 0) + (chunk['embedding_status'] == 'embedded')
    for row in files:
        row.update(chunk_count=chunk_counts.get(row['document_id'], 0),
                   embedded_chunk_count=vector_counts.get(row['document_id'], 0))
    counts = manifest['counts']
    counts.update(listed_files=len(files), collected_files=sum(row['status'] == 'collected' for row in files),
                  excluded_files=sum(row['status'] == 'excluded' for row in files),
                  failed_files=sum(row['status'] == 'failed' for row in files), documents=len(documents),
                  chunks=len(chunks), vectors=0 if vectors is None else len(vectors))
    has_failure = bool(manifest['errors']) or counts['failed_files'] > 0 or not manifest['inventory_complete']
    manifest['status'] = ('partial' if documents else 'failed') if has_failure else (
        'collected_only' if args.collect_only else 'completed')
    print('[5/5] 파일 저장·재로드 검증·HTML 생성', flush=True)
    stage_started = time.perf_counter()
    try:
        save_dataset(folder, files, documents, chunks, vectors)
        validate_dataset(folder, manifest, tokenizer)
    except Exception as error:
        manifest['status'] = 'partial' if documents else 'failed'
        manifest['errors'].append(dict(stage='saving', path=None, code='storage_validation_failed',
                                       message=safe_error(error, token)))
    manifest['timings_sec']['saving'] = time.perf_counter() - stage_started
    manifest['finished_at'] = utc_now()
    manifest['timings_sec']['total'] = time.perf_counter() - started
    # 최종 상태를 먼저 저장하여 집계 화면에도 같은 상태가 나타나게 한다.
    manifest['report_status'] = 'completed'
    write_json(folder / 'manifest.json', manifest)
    report_started = time.perf_counter()
    try:
        html_path, warnings = build_report(output)
        for warning in warnings:
            print('이전 run 조회 경고: ' + warning, file=sys.stderr)
        manifest['timings_sec']['reporting'] = time.perf_counter() - report_started
        manifest['timings_sec']['total'] = time.perf_counter() - started
        write_json(folder / 'manifest.json', manifest)
        build_report(output)
        print(f'HTML: {html_path}')
    except Exception as error:
        manifest.update(status='partial' if documents else 'failed', report_status='failed')
        manifest['errors'].append(dict(stage='reporting', path=None, code='report_failed', message=safe_error(error, token)))
        write_json(folder / 'manifest.json', manifest)
    print(f'Run: {folder}')
    print(f'Status: {manifest["status"]} · 수집 {counts["collected_files"]} · 제외 {counts["excluded_files"]}'
          f' · 실패 {counts["failed_files"]} · 청크 {counts["chunks"]} · 벡터 {counts["vectors"]}')
    for error in manifest['errors']:
        print(f'{error["stage"]} / {error["code"]}: {error["message"]}', file=sys.stderr)
    if manifest['status'] in ('failed', 'partial'):
        return exit_code or 1
    return 0


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        repo = parse_repo_url(args.repo_url)
        validate_path(args.scope, allow_empty=True)
        if args.ref == '':
            raise ValueError('--ref는 빈 문자열일 수 없습니다.')
        token = load_env(args.env_file)
    except ValueError as error:
        parser.error(str(error))
    try:
        return execute(args, repo, token)
    except Exception as error:
        print('실행 실패: ' + safe_error(error, token), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
