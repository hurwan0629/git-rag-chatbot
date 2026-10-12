---
title: Git RAG API 목차와 계약
status: implemented
version: "0.1"
date: 2026-10-10
tags: [git-rag, api, schemas]
---

# API 목차와 계약

[[docs/project/index|서비스 설계 시작]] · [[docs/project/database|DB 스키마]] · [[docs/project/workflows|호출 순서]]

> [!info] Swagger와의 관계
> 여기에는 구현한 API의 목차와 입출력 계약을 적습니다. 요청 Pydantic 모델에서 OpenAPI를 생성하고 `/docs`에서 Swagger UI를 제공합니다. 현재 테스트 서버의 `/api/repos`, `/api/search`, `/api/answer`와는 별도의 **서비스 API 계약**입니다.

## API 목차

```text
Base path: /api/v1
Content-Type: application/json
Encoding: UTF-8
```

| Method | 경로 | 설명 |
| --- | --- | --- |
| GET | [[#서비스 상태\|/health]] | DB·모델·실행기 준비 여부 |
| POST | [[#저장소 등록\|/repos]] | repo URL 등록 |
| GET | [[#저장소 목록\|/repos]] | 선택 가능한 repo와 활성 run |
| POST | [[#수집 시작\|/repos/{repo_id}/ingestions]] | 수집 job 생성 |
| GET | [[#작업 상태\|/jobs/{job_id}]] | 단계·개수·오류 조회 |
| GET | [[#실행 결과와 manifest\|/runs/{run_id}]] | 서비스 manifest 조회 |
| GET | [[#파일 목록\|/runs/{run_id}/files]] | 수집·제외·실패 파일 목록 |
| GET | [[#원문과 청크\|/documents/{run_id}/{document_id}]] | 전체 원문과 청크 경계 |
| POST | [[#검색\|/search]] | 질문과 관련된 청크 검색 |
| POST | [[#답변 생성\|/answers]] | 서버 검색 후 근거 기반 답변 생성 |

## 공통 계약

성공과 오류 형식은 다음과 같습니다. `request_id`는 요청 추적용 UUID이며 job_id와 다릅니다.

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "status": "ok",
  "data": {}
}
```

```json
{
  "request_id": "00000000-0000-4000-8000-000000000001",
  "status": "error",
  "error": {"code": "QUESTION_TOO_LONG", "message": "질문은 E5 기준 500 token 이하여야 합니다."}
}
```

```text
공통 검증
- 요청 스키마에 없는 필드와 잘못된 타입 거부
- Boolean을 정수로 취급하지 않음
- POST 본문 최대 16 KiB
- 빈 질문, 잘못된 UTF-8, NaN, Infinity 거부
- repo_id / job_id: UUID 문자열
- document_id / chunk_id: 64자리 SHA-256 hex 문자열
- run_id: 생성 규칙을 검증한 문자열
- 서버가 알고 있는 DB ID만 조회, 임의의 로컬 파일 경로 입력 없음
- 422 필드 검증 오류도 공통 오류 envelope로 변환

HTTP 오류
400  INVALID_JSON / INVALID_REQUEST
     JSON 또는 요청 형식 오류
404  NOT_FOUND
     repo / job / run / document 없음
409  BUSY / INGESTION_ALREADY_ACTIVE / REPO_NOT_SEARCHABLE
     GPU 사용 중 / 중복 수집 / 검색 불가
413  BODY_TOO_LARGE
     본문 byte 제한 초과
415  UNSUPPORTED_MEDIA_TYPE
     지원하지 않는 본문 형식
422  VALIDATION_ERROR / QUESTION_TOO_LONG / CONTEXT_TOO_LARGE
     필드 검증 또는 token 예산 오류
503  NOT_READY / GENERATION_DISABLED
     미준비 또는 검색 전용 실행
500  INTERNAL_ERROR / GPU_OOM
     처리 오류. 상세 stack과 비밀 값은 응답에 노출하지 않음
```

아래 TypeScript 코드 블록은 **data 내부의 스키마**입니다. `?`는 생략 가능한 필드, `| null`은 NULL 허용을 뜻합니다. JSON 코드 블록은 실제 값의 예시입니다.

```typescript
type UUID = string;
type JsonObject = Record<string, unknown>;
```

## 서비스 상태

```text
GET /api/v1/health
응답: 준비 완료 200 / 미준비 503
```

```typescript
interface HealthData {
  ready: boolean;
  mode: "rag" | "search_only";
  database_ready: boolean;
  encoder_ready: boolean;
  generator_ready: boolean;
  job_runner_ready: boolean;
  device: "cuda" | "cpu";
  generator_dtype: string | null;
}
```

검색 전용 모드에서는 generator_ready=false여도 ready=true일 수 있습니다. lifespan 시작 처리가 끝나기 전에는 HTTP 자체가 준비되지 않을 수 있어, 프론트는 연결 실패도 준비 중 상태로 표시합니다.

## 저장소 등록

```text
POST /api/v1/repos
응답: 신규 등록 201 / 기존 repo 반환 200
```

```json
{"repo_url": "https://github.com/octocat/Hello-World"}
```

```text
repo_url: GitHub HTTPS 루트 URL만 허용
정규화: .git 접미사 제거, repo를 소문자 owner/repo로 저장
```

```typescript
interface Repo {
  repo_id: UUID;
  repo: string;
  repo_url: string;
  active_run_id: string | null;
  searchable: boolean;
}
```

등록은 수집을 실행하지 않습니다. repo 존재·읽기 권한은 수집 단계에서 확인합니다.

## 저장소 목록

```text
GET /api/v1/repos
응답: 200
```

```typescript
interface RepoListItem extends Repo {
  commit_sha: string | null;
  counts: JsonObject;
  unavailable_reason: string | null;
  active_job_id: UUID | null;
  runs: {run_id: string; status: string; commit_sha: string | null; counts: JsonObject; created_at: string}[];
}

interface RepoListData {
  items: RepoListItem[];
}
```

초기에는 전체 repo 목록을 반환하며 목록이 커지면 pagination을 추가합니다. 프론트는 searchable=false를 체크 불가로 표시합니다. active_run_id는 최신 GitHub commit을 뜻하지 않습니다. 검증된 completed / partial 활성 run에 호환 벡터가 1개 이상이면 searchable=true이며, partial의 오류 기록은 유지합니다.

## 수집 시작

```text
POST /api/v1/repos/{repo_id}/ingestions
Path: repo_id = UUID
응답: 202
```

```json
{"ref": null, "scope_path": ""}
```

```typescript
interface IngestionRequest {
  ref?: string | null;
  scope_path?: string;
}

interface IngestionData {
  job_id: UUID;
  run_id: string;
  repo_id: UUID;
  status: "queued";
}
```

```text
ref: 생략 또는 null이면 기본 브랜치, 문자열이면 비어 있지 않고 최대 255자
scope_path: 생략 또는 빈 문자열이면 전체 repo
            지정하면 안전한 상대 POSIX 경로, 최대 4096자
파일 필터 / 모델 / 청킹: 서버의 고정 설정 사용
env 파일 / token: 요청 필드로 받지 않음
동일 repo에 queued 또는 running job 존재: 409 INGESTION_ALREADY_ACTIVE
신규 수집 mode: embed
```

실패 후 재시도도 이 API로 새 run을 만듭니다. collect_only는 기존 결과 import에만 해당합니다.

## 작업 상태

```text
GET /api/v1/jobs/{job_id}
Path: job_id = UUID
응답: 200
```

```json
{
  "job_id": "00000000-0000-4000-8000-000000000002",
  "run_id": "example-run",
  "repo_id": "00000000-0000-4000-8000-000000000003",
  "status": "running",
  "phase": "embedding",
  "progress": {"unit": "chunks", "current": 120, "total": 300, "percent": 40.0},
  "created_at": "2026-10-10T01:00:00Z",
  "started_at": "2026-10-10T01:00:01Z",
  "finished_at": null,
  "error": null
}
```

```text
status: queued | running | completed | failed | interrupted
phase: queued | listing | collecting | chunking | embedding
       | validating | publishing | finished
progress.unit: files | chunks | steps | null
progress.current: 0 이상의 정수
progress.total: 0 이상의 정수 또는 null
progress.percent: total > 0이면 100 * current / total, 나머지는 null
```

상태 정의는 [[docs/project/database#jobs|jobs 스키마]]와 같습니다. 프론트는 약 1~2초 간격으로 조회하고 종료 상태에서 멈춥니다.

## 실행 결과와 manifest

```text
GET /api/v1/runs/{run_id}
Path: run_id = 생성 규칙을 검증한 문자열
응답: 200
data: ServiceManifest (schema_version = "2.0")
```

data는 [[docs/project/artifacts-models#서비스 manifest|서비스 manifest]]입니다. 저장 설정·완료 상태·제외 건수 등을 확인하는 상세 화면에 사용합니다. 원본 `source_manifest` 전체는 응답하지 않습니다.

## 파일 목록

```text
GET /api/v1/runs/{run_id}/files
Path: run_id = 생성 규칙을 검증한 문자열
응답: 200

Query:
  status?: collected | excluded | failed
  path_prefix?: 안전한 상대 경로 문자열, 빈 문자열이면 전체
  limit?: 정수, 기본값 100, 범위 1~500
  offset?: 정수, 기본값 0, 범위 0 이상
```

```typescript
interface FileItem {
  path: string;
  entry_type: string;
  mode: string;
  git_object_sha: string;
  size_bytes: number | null;
  status: "collected" | "excluded" | "failed";
  reason_code: string | null;
  reason: string | null;
  document_id: string | null;
  chunk_count: number;
  embedded_chunk_count: number;
}

interface FileListData {
  items: FileItem[];
  total: number;
  limit: number;
  offset: number;
}
```

prefix에 `src`를 입력하면 `src/` 아래를 조회합니다. 경로 오름차순으로 반환합니다. file과document를 join하고 청크 개수를 집계하며, 원문 조회 링크는 run_id와 document_id를 사용합니다.

## 원문과 청크

```text
GET /api/v1/documents/{run_id}/{document_id}
Path:
  run_id = 생성 규칙을 검증한 문자열
  document_id = 64자리 SHA-256 hex 문자열
응답: 200
```

```typescript
interface ChunkBoundary {
  chunk_id: string;
  chunk_index: number;
  start_offset: number;
  end_offset: number;
  start_line: number;
  end_line: number;
  token_count: number;
  embedding_status: "not_run" | "embedded" | "failed";
}

interface DocumentData {
  run_id: string;
  document_id: string;
  repo: string;
  commit_sha: string;
  path: string;
  text: string;
  line_count: number;
  content_sha256: string;
  source_url: string;
  chunks: ChunkBoundary[];
}
```

본문은 전체 text에서 offset으로 잘라 표시하고, 브라우저에서 전체 문서·선택 청크를 함께 볼 수 있게 합니다. 응답에 벡터 숫자 배열을 포함하지 않습니다.

## 검색

```text
POST /api/v1/search
응답: 200
```

```json
{
  "question": "이 저장소는 어떻게 실행하나요?",
  "repo_ids": ["00000000-0000-4000-8000-000000000003"],
  "top_k": 5
}
```

```typescript
interface SearchRequest {
  question: string;
  repo_ids: UUID[];
  top_k?: number;
}

interface Source {
  source_id: string; // 요청 안에서 S1, S2, ...
  repo_id: UUID;
  repo: string;
  run_id: string;
  commit_sha: string;
  document_id: string;
  chunk_id: string;
  score: number; // cosine similarity
  path: string;
  start_line: number;
  end_line: number;
  start_offset: number;
  end_offset: number;
  text: string;
  source_url: string; // 고정 commit의 GitHub URL
}

interface TimingsMs {
  query_encoding: number;
  search: number;
  context: number;
  generation: number;
  total: number;
}

interface SearchData {
  question: string;
  question_tokens: number; // prefix·special을 제외한 E5 본문 token 수
  selected_runs: { repo_id: UUID; run_id: string; commit_sha: string }[];
  sources: Source[];
  timings_ms: TimingsMs;
}
```

```text
question: 앞뒤 공백 제거 후 비어 있지 않은 문자열
          E5 본문 500 token 이하
          query prefix·special 포함 512 token 이하
repo_ids: 중복 없는 UUID 배열, 1개 이상
          모든 repo가 존재하고 검색 가능해야 함
top_k: 정수 1~10, 생략 시 5
token 검증: 서버의 실제 tokenizer 사용, 자동 truncation 없음
```

기존 파일의 vector_row는 서비스 Source에서 제거합니다. 순위는 score 내림차순, 동률은 run_id·chunk_id 오름차순입니다. 동일 chunk_id가 여러 선택repo에 나타나면 가장 앞선 결과 하나만 남기고 최대 K개를 반환합니다. 부족하면 실제 개수만 반환합니다.

## 답변 생성

```text
POST /api/v1/answers
응답: 200
```

```typescript
type AnswerRequest = SearchRequest;
```

서버가 검색을 다시 수행합니다. 클라이언트가 임의의 sources나 system prompt를 보내도록 하지 않습니다.

```typescript
interface Generation {
  model_id: string;
  revision: string;
  prompt_version: string;
  input_tokens: number;
  output_tokens: number;
  finish_reason: "eos" | "length";
}

interface AnswerData extends SearchData {
  answer_status: "generated" | "invalid_citation" | "no_evidence";
  answer: string;
  context_source_ids: string[]; // Qwen에 실제 전달한 근거 ID
  cited_source_ids: string[]; // 답변의 유효한 인용 ID
  invalid_citation_ids: string[]; // 미존재 또는 전달하지 않은 근거 ID
  generation: Generation | null; // 생성 미실행 시 null
}
```

sources는 검색 결과이고, 예산 때문에 생성 입력에서 빠진 source가 있을 수 있습니다. 인용은 context_source_ids를 기준으로 확인합니다.

검색 결과가 비면 모델을 호출하지 않고 no_evidence로 응답합니다. 단, Top K가 있다는 사실만으로 답변 가능한 근거가 있다고 판단하지 않습니다. 관련성이 부족하면 Qwen이 확인 불가를 설명하도록 기존 prompt 규칙을 유지합니다.

```text
timings_ms: 밀리초 단위, 실행하지 않은 단계는 0
total: 대기·검증·DB 역참조 등을 포함한 전체 시간
       query_encoding + search + context + generation과 같을 필요는 없음
```
