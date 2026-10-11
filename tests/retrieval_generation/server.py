"""저장된 repo를 검색하고 근거 답변을 생성하는 로컬 HTTP 서버."""

import argparse
import importlib.metadata
import json
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'retrieval_generation'

from repo_ingestion.storage import write_json
from . import APIError, error_response
from .catalog import Catalog, EMBEDDING
from .prompts import INPUT_BUDGET, MAX_NEW_TOKENS, PROMPT_VERSION
from .retriever import Retriever

BASE_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 16 * 1024


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def validate_input(payload, catalog):
    if not isinstance(payload, dict) or set(payload) - {'question', 'repos', 'top_k'}:
        raise APIError(400, 'INVALID_INPUT', 'question, repos, top_k로 구성한 JSON 객체가 필요합니다.')
    question, repos, top_k = payload.get('question'), payload.get('repos'), payload.get('top_k', 5)
    if not isinstance(question, str) or not question.strip():
        raise APIError(400, 'INVALID_QUESTION', '질문을 입력하세요.')
    if not isinstance(repos, list) or not repos or any(not isinstance(repo, str) for repo in repos):
        raise APIError(400, 'INVALID_REPOS', '검색할 저장소를 1개 이상 선택하세요.')
    if len(set(repos)) != len(repos) or any(repo not in catalog.runs for repo in repos):
        raise APIError(400, 'INVALID_REPOS', '중복되거나 검색할 수 없는 저장소가 선택되었습니다.')
    if type(top_k) is not int or not 1 <= top_k <= 10:
        raise APIError(400, 'INVALID_TOP_K', 'top_k는 1~10의 정수여야 합니다.')
    return dict(question=question.strip(), repos=repos, top_k=top_k)


class App:
    def __init__(self, catalog, retriever, generator=None, output_dir=BASE_DIR / 'output'):
        self.catalog, self.retriever, self.generator = catalog, retriever, generator
        self.work_lock, self.log_lock = threading.Lock(), threading.Lock()
        self.session_id = uuid.uuid4().hex
        self.session_dir = Path(output_dir) / self.session_id
        self.mode = 'rag' if generator else 'search_only'
        libraries = dict(python=sys.version.split()[0])
        for package in ('torch', 'transformers', 'numpy', 'huggingface-hub'):
            libraries[package] = importlib.metadata.version(package)
        generation = None
        if generator:
            from .generator import MODEL_ID
            generation = dict(model_id=MODEL_ID, revision=generator.revision, dtype='bfloat16')
        runs = [dict(repo=r['repo'], run_id=r['run_id'], commit_sha=r['commit_sha'])
                for r in catalog.repos if r['searchable']]
        write_json(self.session_dir / 'session.json', dict(
            schema_version='1.0', session_id=self.session_id, started_at=utc_now(), mode=self.mode,
            catalog_id=catalog.catalog_id, runs=runs,
            embedding={key: EMBEDDING[key] for key in ('model_id', 'revision', 'pooling', 'normalize', 'dimension')},
            generation=generation, settings=dict(top_k=5, question_tokens=500, input_tokens=INPUT_BUDGET,
                                                  max_new_tokens=MAX_NEW_TOKENS, prompt_version=PROMPT_VERSION),
            libraries=libraries, device=retriever.device))

    def record(self, request_id, endpoint, payload, response, messages=None):
        record = dict(schema_version='1.0', session_id=self.session_id, request_id=request_id,
                      created_at=utc_now(), endpoint=endpoint,
                      input=payload if isinstance(payload, dict) else None,
                      response=response, messages=messages)
        try:
            line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
            with self.log_lock, (self.session_dir / 'requests.jsonl').open('a', encoding='utf-8', newline='\n') as stream:
                stream.write(line)
                stream.flush()
        except (OSError, UnicodeError, TypeError, ValueError, RecursionError):
            raise APIError(500, 'LOG_WRITE_FAILED', '요청 기록을 저장하지 못했습니다. 서버 콘솔을 확인하세요.') from None

    def post(self, endpoint, payload, request_id):
        acquired, messages = False, None
        started = time.perf_counter()
        try:
            if endpoint not in ('/api/search', '/api/answer'):
                raise APIError(404, 'NOT_FOUND', '요청 경로를 찾을 수 없습니다.')
            inputs = validate_input(payload, self.catalog)
            if endpoint == '/api/answer' and self.generator is None:
                raise APIError(503, 'GENERATION_DISABLED', '검색 전용 모드입니다. 서버를 일반 모드로 다시 실행하세요.')
            acquired = self.work_lock.acquire(blocking=False)
            if not acquired:
                raise APIError(409, 'BUSY', '다른 검색 또는 답변 생성 작업이 진행 중입니다.')
            response = self.retriever.search(**inputs)
            response.update(request_id=request_id, catalog_id=self.catalog.catalog_id)
            if endpoint == '/api/answer':
                if response['results']:
                    answer, messages, timings = self.generator.answer(inputs['question'], response['results'])
                    response.update(answer)
                    response['timings_ms'].update(timings)
                else:
                    response.update(answer_status='no_evidence', answer='저장된 자료에서 답변 근거를 확인할 수 없습니다.',
                                    context_source_ids=[], cited_source_ids=[], invalid_citation_ids=[], generation=None)
            response['timings_ms']['total'] = (time.perf_counter() - started) * 1000
            status = 200
        except APIError as error:
            status, response = error.status, error_response(request_id, error)
        except Exception as error:
            traceback.print_exc()
            if 'out of memory' in str(error).lower():
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                failure = APIError(500, 'MODEL_OOM', 'GPU 메모리가 부족합니다. 검색 전용 모드 또는 더 작은 컨텍스트를 확인하세요.')
            else:
                failure = APIError(500, 'INTERNAL_ERROR', '처리에 실패했습니다. 서버 콘솔을 확인하세요.')
            status, response = failure.status, error_response(request_id, failure)
        try:
            self.record(request_id, endpoint, payload, response, messages)
        except APIError as error:
            status, response = error.status, error_response(request_id, error)
        finally:
            if acquired:
                self.work_lock.release()
        return status, response


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def send_payload(self, status, payload, content_type='application/json; charset=utf-8'):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url, app = urlsplit(self.path), self.server.app
        request_id = uuid.uuid4().hex
        try:
            if url.path == '/' and not url.query:
                self.send_payload(200, (BASE_DIR / 'web/index.html').read_bytes(), 'text/html; charset=utf-8')
            elif url.path == '/api/repos' and not url.query:
                self.send_payload(200, dict(**app.catalog.snapshot(), mode=app.mode, session_id=app.session_id))
            elif url.path == '/api/document':
                params = parse_qs(url.query, keep_blank_values=True)
                if set(params) != {'run_id', 'document_id'} or any(len(v) != 1 or not v[0] for v in params.values()):
                    raise APIError(404, 'DOCUMENT_NOT_FOUND', 'run_id와 document_id로 문서를 조회하세요.')
                self.send_payload(200, app.catalog.document(params['run_id'][0], params['document_id'][0]))
            else:
                raise APIError(404, 'NOT_FOUND', '요청 경로를 찾을 수 없습니다.')
        except APIError as error:
            self.send_payload(error.status, error_response(request_id, error))
        except Exception:
            traceback.print_exc()
            self.send_payload(500, error_response(request_id, APIError(500, 'INTERNAL_ERROR', '조회에 실패했습니다.')))

    def do_POST(self):
        request_id, payload = uuid.uuid4().hex, None
        endpoint = urlsplit(self.path).path
        try:
            if endpoint not in ('/api/search', '/api/answer') or urlsplit(self.path).query:
                raise APIError(404, 'NOT_FOUND', '요청 경로를 찾을 수 없습니다.')
            if (self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding')
                    or self.headers.get_content_charset('utf-8').lower() != 'utf-8'):
                raise APIError(400, 'INVALID_JSON', 'Content-Length가 있는 UTF-8 application/json 요청이 필요합니다.')
            try:
                size = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                raise APIError(400, 'INVALID_JSON', 'Content-Length가 유효하지 않습니다.') from None
            if size > MAX_BODY_BYTES:
                raise APIError(413, 'BODY_TOO_LARGE', '요청 본문은 16 KiB 이하여야 합니다.')
            if size <= 0:
                raise APIError(400, 'INVALID_JSON', 'JSON 본문이 필요합니다.')
            try:
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise ValueError('불완전한 body')
                def invalid_constant(_):
                    raise ValueError('NaN/Infinity는 JSON 값이 아닙니다.')
                payload = json.loads(raw.decode('utf-8'), parse_constant=invalid_constant)
                json.dumps(payload, ensure_ascii=False).encode('utf-8')
            except (ValueError, UnicodeError, TimeoutError, RecursionError):
                payload = None
                raise APIError(400, 'INVALID_JSON', '올바른 UTF-8 JSON을 보내세요.') from None
        except APIError as error:
            response = error_response(request_id, error)
            try:
                self.server.app.record(request_id, endpoint, payload, response)
            except APIError as log_error:
                error, response = log_error, error_response(request_id, log_error)
            self.send_payload(error.status, response)
            return
        status, response = self.server.app.post(endpoint, payload, request_id)
        self.send_payload(status, response)


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            # Windows SO_REUSEADDR는 실행 중인 서버의 포트도 중복 바인딩할 수 있다.
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(app, port=8000):
    server = LocalHTTPServer(('127.0.0.1', port), Handler)
    server.app = app
    return server


def port_number(value):
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('포트는 1~65535의 정수여야 합니다.') from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('포트는 1~65535의 정수여야 합니다.')
    return port


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     epilog='입력: tests/repo_ingestion/output/ · 접속: http://127.0.0.1:<PORT>/ · 종료: Ctrl+C')
    parser.add_argument('--port', type=port_number, default=8000, help='로컬 서버 포트')
    parser.add_argument('--search-only', action='store_true', help='E5 검색만 실행; Qwen 다운로드·로딩 생략')
    args = parser.parse_args(argv)
    server = None
    try:
        # 포트 충돌을 모델 로딩 전에 확인해 같은 GPU에 모델을 중복 로딩하지 않는다.
        server = make_server(None, args.port)
        print('[1/3] 저장된 run 탐색·검증', flush=True)
        catalog = Catalog()
        for repo in catalog.repos:
            print(f"  {repo['repo']}: {repo['chunks']} chunks" if repo['searchable']
                  else f"  {repo['repo']}: 제외 · {repo['reason']}", flush=True)
        if not catalog.runs:
            raise ValueError('검색 가능한 완료 run이 없습니다. 기존 수집 결과를 확인하세요.')
        print('[2/3] E5 질문 encoder 로딩', flush=True)
        retriever = Retriever(catalog)
        generator = None
        if not args.search_only:
            print('[3/3] Qwen 다운로드·로딩 (처음에는 시간이 걸립니다)', flush=True)
            from .generator import Generator
            generator = Generator()
        else:
            print('[3/3] 검색 전용 · 답변 모델 로딩 생략', flush=True)
        app = App(catalog, retriever, generator)
        server.app = app
        print(f'준비 완료: http://127.0.0.1:{args.port}/ · {app.mode} · {retriever.device}', flush=True)
        print(f'실행 기록: {app.session_dir}', flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n서버를 종료합니다.', flush=True)
    except Exception as error:
        print(f'시작 실패: {error}', file=sys.stderr, flush=True)
        return 1
    finally:
        if server:
            server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
