---
title: Git RAG manifest와 모델 저장
status: implemented
version: "0.1"
date: 2026-10-10
tags: [git-rag, manifest, storage, models]
---

# manifest와 모델 저장

[[docs/project/index|서비스 설계 시작]] · [[docs/project/database|ERD]] · [[docs/project/workflows|데이터 흐름]]

> [!abstract] 핵심
> 현재 테스트 파일은 데이터와 모델 실행을 검증하기 위한 저장 형식입니다. 서비스에서는 DB가 기준이고 manifest는 **run 정보를 모아 반환하는 요약**이 됩니다. 모델 가중치, 문서 벡터, 실행 요약은 각각 다른 자원입니다.

## 목차

- [[#현재 파일을 이해하기|현재 파일을 이해하기]]
- [[#파일에서 DB로 옮기는 방법|파일에서 DB로 옮기는 방법]]
- [[#서비스 manifest|서비스 manifest]]
- [[#모델 선택과 고정 설정|모델 선택과 고정 설정]]
- [[#모델 파일의 저장과 로딩|모델 파일의 저장과 로딩]]
- [[#모델 교체와 데이터 변경|모델 교체와 데이터 변경]]

## 현재 파일을 이해하기

```text
tests/repo_ingestion/output/
├─ index.html                   # 모든 run을 모은 독립 조회 HTML
└─ owner--repo/run_id/
   ├─ manifest.json             # 실행 요약
   ├─ files.csv                 # 발견한 파일, 제외·실패 포함
   ├─ documents.jsonl           # 파일 하나의 전체 원문 = 객체 하나
   ├─ chunks.jsonl              # 원문 구간과 vector_row
   ├─ vectors.npy               # float32 (N,384), ID 없는 숫자 행렬
   └─ raw/document_id.txt       # 직접 읽기용 원문 중복 저장
```

| 현재 파일 | 의미 | 서비스에서의 대응 |
| --- | --- | --- |
| manifest.json | run 출처·설정·상태·개수·오류 | runs + repo + profile을 join한 요약 |
| files.csv | 파일별 처리 상태 | files, 문서 연결·개수는 조회 시 계산 |
| documents.jsonl | 파일 전체 원문 | documents. repo/commit은 run을 통해 조회 |
| chunks.jsonl | 벡터화 대상과 원문 구간 | chunks. vector_row는 DB에 저장하지 않음 |
| vectors.npy | 청크 벡터 행렬 | embeddings.embedding |
| raw TXT | JSONL과 동일한 원문 사본 | 서비스에서는 DB 원문 사용. 사본 저장은 선택 |
| index.html | 저장 파일을 모아 만든 조회 산출물 | React 화면이 API를 통해 조회 |

`documents.jsonl`의 한 줄은 JSON 객체 하나입니다. 원문을 실제 줄마다 별도 document로 만드는 뜻이 아닙니다. `vectors.npy`에는 document_id나chunk_id가 없으며, `chunks.jsonl`의 vector_row로 연결합니다. 원문은 [[docs/tests/repo-ingestion/final-design#6. 파일별 스키마|기존 파일 스키마]]에서 확인합니다.

## 파일에서 DB로 옮기는 방법

1. run 폴더를 발견하고 manifest schema 1.0을 확인합니다. 실제 artifacts 경로는 허용한 run 폴더 내부로 제한합니다.
2. 기존 저장 검증 함수로 원문 bytes·hash·ID·청크 구간·벡터 연결·건수를 검증하고, 완료 run의 모델 호환성을 확인합니다.
3. repo와 고정 embedding profile을 등록합니다. 본문·청크가 없는 실패 run은 현재 profile에 연결하되 원본 manifest의 revision NULL을 보존합니다. 데이터가 있는데 revision이 없거나 모델 규칙이 다르면 import를 거부합니다.
4. 원래 run_id를 보존해 files → documents → chunks → embeddings 순서로 트랜잭션 안에 저장합니다. NPY는 allow_pickle=False로 읽습니다.
5. 각 embedded chunk의 vector_row에 해당하는 벡터를 `(run_id,chunk_id,profile_id)` 행으로 넣습니다.
6. 원래 manifest는 source_manifest로 보존하고 서비스 run schema_version은 2.0으로 기록합니다. 모델 설정은 profile, filter/chunking/counts/errors는 대응 컬럼으로 옮깁니다.
7. repo별 가장 최근의 **검증된 완료 embed run**을 활성화합니다. 최신 완료 run이 손상·호환 불일치·0벡터이면 이전 run으로 자동 대체하지 않고 이유를 표시합니다.

run_id가 이미 존재하면 원본과 동일한지 확인하고 재삽입하지 않습니다. 같은 ID에 다른 내용이면 충돌 오류입니다. import 실패는 해당 run의 DB 저장을 rollback하고 원본 파일은 유지합니다.

manifest의 report_status/artifacts/directories는 source_manifest에 남깁니다. 서비스에서는 HTML 생성 상태나 원본 출력 경로를 검색 조건으로 사용하지 않습니다. 폴더 트리는 files의path에서 구성합니다. 본문·vector 없는 실패 run은 보존 가능한 수준으로만 import하고 활성화하지 않습니다.

> [!note] 두 저장소를 동시에 수정하지 않기
> import 후 서비스는 DB만 읽고 씁니다. 기존 파일은 테스트 기록이며 DB와 자동 양방향 동기화하지 않습니다. export가 필요하면 DB에서 다시 생성합니다. export의 vector_row는 그 export 내부에서 새로 부여합니다.

## 서비스 manifest

`GET /api/v1/runs/{run_id}`의 data를 아래 형태로 반환합니다. DB의 여러 테이블을 모은 **조회용 객체**이고 별도 manifest 테이블은 만들지 않습니다.

```json
{
  "schema_version": "2.0",
  "run_id": "example-run",
  "repo_id": "00000000-0000-4000-8000-000000000003",
  "repo": "octocat/hello-world",
  "repo_url": "https://github.com/octocat/hello-world",
  "ref_requested": null,
  "commit_sha": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
  "tree_sha": null,
  "scope_path": "",
  "mode": "embed",
  "status": "running",
  "active": false,
  "searchable": false,
  "inventory_complete": false,
  "auth_used": false,
  "created_at": "2026-10-10T01:00:00Z",
  "started_at": "2026-10-10T01:00:01Z",
  "finished_at": null,
  "filter": {"max_file_bytes": 1000000, "extensions": [".py", ".md", ".ts"]},
  "chunking": {"method": "line_greedy_char_fallback", "max_input_tokens": 512, "overlap_tokens": 0, "offset_unit": "python_char"},
  "embedding": {
    "profile_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "model_id": "intfloat/multilingual-e5-small",
    "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
    "passage_prefix": "passage: ",
    "query_prefix": "query: ",
    "pooling": "masked_mean",
    "normalize": true,
    "dimension": 384,
    "dtype": "float32",
    "max_input_tokens": 512
  },
  "counts": {"listed_files": 0, "collected_files": 0, "excluded_files": 0, "failed_files": 0, "documents": 0, "chunks": 0, "vectors": 0},
  "timings_sec": {},
  "execution": {"batch_size": 8, "requested_device": "auto", "device": null, "encoder_loaded": false},
  "libraries": {},
  "errors": []
}
```

예시의 run_id/profile_id는 설명용 임시 값입니다. 실제로는 생성 규칙에 따라 계산합니다. filter 예시는 짧게 표시했으며 실제 응답에는 기존 filter 설정의 전체 키를 포함합니다.

| 항목 | 조건 |
| --- | --- |
| schema_version | 서비스는2.0. 기존 파일1.0과 구분 |
| repo·URL | repositories에서 조회 |
| ref·commit·scope·status·시간·설정 | ingestion_runs에서 조회. 확정 전 SHA, 시작 전 started, 종료 전 finished는 NULL |
| embedding | embedding_profiles에서 조회. model_id와 revision을 표시 |
| execution | runs.execution_settings에서 조회. batch_size≥1, requested_device는 auto/cpu/cuda 또는 기존import의 NULL, device는 NULL 또는 실제 장치, encoder_loaded는 boolean |
| active | repo의 active_run_id와 현재 run_id가 일치 |
| searchable | 이 run이 active이며 completed·embed·벡터 존재·profile 호환 |
| counts | 처리 중에는 중간 집계, 완료 시에는 실제 테이블 건수와 일치 |
| errors | stage/path/code/message. path만 NULL 허용. 토큰 값 제거 |

batch_size/device/encoder_loaded 등의 실행 조건은 profile과 별도로 기록합니다. 기존1.0 embedding 안의 실행 조건은 import 시 execution_settings로 옮기고, 없는 requested_device는 source_manifest의 정보만으로 추측하지 않아 NULL을 허용합니다. 신규run에서는 요청 장치를 반드시 기록합니다. Qwen 정보는 run의 embedding에 섞지 않고 답변 응답과 서버 설정에 표시합니다.

## 모델 선택과 고정 설정

| 용도 | 모델 | 고정revision | 로딩 클래스 |
| --- | --- | --- | --- |
| 문서·질문 인코더 | [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small) | `614241f622f53c4eeff9890bdc4f31cfecc418b3` | AutoTokenizer + AutoModel |
| 답변 생성 | [Qwen/Qwen3-4B-Instruct-2507](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507) | `cdbee75f17c01a7cc42f958dc650907174af0554` | AutoTokenizer + AutoModelForCausalLM |

질문에 따라 모델을 자동 선택하지 않습니다. E5는 masked mean pooling·L2 정규화, 저장float32, 384차원입니다. Qwen의 greedy 생성은 유지하고 서비스의 실행 장치는 아래 auto 규칙을 사용합니다. 현재 테스트는 CUDA/BF16 전용이며 서비스는 CPU FP32 생성도 지원합니다. 실제 확인 범위는 [[docs/project/validation|검증 기록]]을 봅니다.

```python
device = "cuda" if torch.cuda.is_available() else "cpu"
dtype = (
    torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported()
    else torch.float16 if device == "cuda"
    else torch.float32
)
```

위 dtype은 생성 모델 가중치 로딩 기준입니다. E5 벡터의 최종 저장 dtype은 어느 장치에서도 float32입니다. 장치 자동 선택은 사용 가능 여부를 판단하며 여유 VRAM까지 보장하지 않습니다. CUDA OOM은 오류를 표시하고 CPU 실행을 선택할 수 있게 합니다. CPU에서는 생성 시간이 길고 Qwen FP32 가중치에 약16 GB와 추가 실행 메모리가 필요합니다.

| 예산·설정 | 초기값 |
| --- | --- |
| E5 passage 입력 | prefix·special 포함512 token 이하 |
| E5 질문 | 본문500 이하, query prefix·special 포함512 이하 |
| Qwen 입력 | chat template 포함3584 token 이하 |
| Qwen 생성 | max_new_tokens=512 |
| prompt_version | 현재 테스트1.1을 기준으로 이식 |
| trust_remote_code | false |

E5와 Qwen의 tokenizer는 다릅니다. E5의500 token 제한만으로 Qwen 입력이 맞는다고 판단하지 않고 생성 쪽에서도 전체 입력을 검증합니다. 이 예산은 서비스의 설정이며 모델 자체의 최대 context 길이를 의미하지 않습니다.

## 모델 파일의 저장과 로딩

```text
data/
├─ model-cache/                 # HF cache의 내부 구조는 라이브러리에 맡김
│  ├─ models--intfloat--multilingual-e5-small/
│  └─ models--Qwen--Qwen3-4B-Instruct-2507/
└─ exports/                     # 필요한 경우 run export
```

1. 설정에 model_id와 revision을 고정하고 배포 전에 가중치·tokenizer·config를 받습니다.
2. 다운로드는 `cache_dir`로 model-cache를 지정합니다. tokenizer와 model은 같은 revision을 읽습니다. 현재 PC에 이미 있는 테스트 캐시도 읽어 중복 다운로드를 줄입니다.
3. 시작 시 `local_files_only=True`로 로드하며, 필요한 파일이 없으면 `manage.py download-models` 안내와 함께 시작 오류로 처리합니다. 요청 중에 다운로드하지 않습니다.
4. FastAPI lifespan에서 각 모델을 한 번 로드하고 eval/inference_mode로 실행합니다.

HF cache는 여러 revision을 보존할 수 있습니다. 캐시 디렉터리 구조를 직접 만들거나 실행 중인 이전 revision을 삭제하지 않습니다. [Hugging Face revision 지정과 캐시](https://huggingface.co/docs/huggingface_hub/en/guides/download)

`.env`는 backend 시작 시 설정으로 읽습니다. 현재 `GITHUG_TOKEN`을 우선하고 `GITHUB_TOKEN`을 fallback으로 사용하며 값을 DB·manifest·로그·프론트에 내보내지 않습니다. 기존 load_env의 동작을 옮기고 GitHub token을 모델 다운로드에 사용하지 않습니다.

## 모델 교체와 데이터 변경

| 변경 | 필요한 작업 |
| --- | --- |
| Qwen 또는 system prompt | 설정·prompt version 변경, 모델 재로딩, 답변 평가. 문서 벡터 재생성 불필요 |
| E5 model/revision/tokenizer/pooling/prefix | 새profile과 새run으로 재청킹·임베딩. 질문 쪽도 같은 profile로 교체 |
| 청킹 방식·크기·겹침 | 새run에서 재청킹·임베딩 |
| repo 커밋·수집 범위·필터 | 새run 수집. 과거run은 출처 확인용으로 유지 |
| 벡터 차원 | vector(384) 스키마와 API 호환성 검토·migration 필요 |

profile 교체 시 모든 선택repo가 검색 가능한 상태인지 확인하고 전환합니다. 과거profile의 active run이 있으면 검색 불가 이유를 표시하며 새 질문 벡터와 비교하지 않습니다.
