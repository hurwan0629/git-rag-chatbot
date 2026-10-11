---
title: Git RAG 실행과 사용 방법
status: implemented
date: 2026-10-10
tags: [git-rag, usage, help]
---

# 실행과 사용 방법

[[docs/project/index|서비스 문서]] · [[docs/project/database-setup|DB 환경 설정]] · [[docs/project/api|API 계약]]

## 목차

- [[#최초 준비|최초 준비]]
- [[#서버 실행 help|서버 실행 help]]
- [[#관리 명령 help|관리 명령 help]]
- [[#화면 사용|화면 사용]]
- [[#개발과 검증|개발과 검증]]
- [[#문제가 생겼을 때|문제가 생겼을 때]]

## 최초 준비

프로젝트 루트에서 실행합니다. 현재 PC의 Python은 아래 경로를 사용합니다. clone한 PC에서는 Python 3.12 venv를 만들고 변수만 바꿉니다.

```powershell
$ragPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $ragPython -m pip install -r backend/requirements.txt
```

`.env.example`과 같은 형식으로 루트 `.env`를 채웁니다. 이미 `.env`가 있으면 복사로 덮어쓰지 않습니다.

| 값 | 사용 |
| --- | --- |
| POSTGRES_PASSWORD | 필수. Docker와 backend가 같은 값을 사용 |
| GITHUG_TOKEN | GitHub API 인증. 기존 이름 유지, GITHUB_TOKEN fallback 지원 |
| MODEL_DEVICE | auto / cuda / cpu. 기본 auto |
| SEARCH_ONLY | true이면 Qwen을 로드하지 않고 검색만 사용 |
| MODEL_CACHE_DIR | 모델 캐시 경로. 기본 ./data/model-cache |

```powershell
docker compose up -d --wait db
& $ragPython backend/manage.py migrate
& $ragPython backend/manage.py download-models

Push-Location frontend
npm ci
npm run build
Pop-Location
```

현재 PC는 기존 테스트의 E5/Qwen 캐시를 읽을 수 있어서 다운로드 없이 시작할 수 있습니다. 새 다운로드는 지정한 공용 캐시에 저장합니다. 시작 시에는 로컬 파일만 읽고 HTTP 요청 처리 중 다운로드하지 않습니다.

기존 output을 DB로 옮기려면 다음을 실행합니다. 모든 run을 검사하며 완료 run만 검색 대상으로 활성화합니다. 실패 run도 기록은 보존합니다.

```powershell
& $ragPython backend/manage.py import-output
```

## 서버 실행 help

```text
usage: python backend/run.py [-h] [--port PORT] [--device {auto,cpu,cuda}] [--search-only]

-h, --help      도움말
--port PORT     localhost HTTP 포트. 기본 8000, 범위 1~65535
--device        .env MODEL_DEVICE보다 우선. auto는 CUDA 사용 가능 여부로 선택
--search-only   Qwen 없이 검색만 실행. .env SEARCH_ONLY=true로도 선택 가능

host: 127.0.0.1
workers: 1
frontend: frontend/dist를 FastAPI가 제공
```

```powershell
& $ragPython backend/run.py
& $ragPython backend/run.py --device cpu --search-only --port 8001
```

기본 주소는 `http://127.0.0.1:8000`, Swagger는 `/docs`입니다. 모델 로딩이 끝나고 `Service ready`가 출력되면 접속합니다. Ctrl+C로 서버를 종료해도 DB volume과 모델 캐시는 유지됩니다.

## 관리 명령 help

```text
usage: python backend/manage.py [-h] {migrate,import-output,download-models} ...

migrate
  Alembic upgrade head. 다시 실행해도 이미 적용된 migration은 반복하지 않음

import-output [OUTPUT]
  기본: tests/repo_ingestion/output
  CSV / JSONL / NPY / raw 원문을 검증 후 DB에 저장
  동일 run_id + 동일 내용: skip
  동일 run_id + 다른 내용: 충돌 오류
  실패한 run의 삽입은 rollback. 원본 파일 유지

download-models [--encoder-only]
  고정 E5 / Qwen revision을 MODEL_CACHE_DIR에 다운로드
  --encoder-only: 검색 전용 PC에서 E5만 준비
```

```powershell
& $ragPython backend/manage.py import-output 'C:\example\output'
& $ragPython backend/manage.py download-models --encoder-only
```

## 화면 사용

1. **등록 + 수집 시작**: GitHub URL을 입력합니다. ref와 폴더 범위는 선택 사항입니다. 저장소 등록과 job 생성 API를 차례로 호출합니다.
2. **수집 진행도**: 단계와 개수·%를 확인합니다. 총량이 없는 단계는 진행률을 계산하지 않습니다.
3. **검색할 저장소**: 호환되는 활성 벡터가 있는 저장소만 체크할 수 있습니다. 새 수집 실패 시 이전 활성 run은 유지합니다.
4. **질문**: 검색은 E5 + pgvector, 답변 생성은 검색 후 Qwen을 사용합니다. 본문 500 token 제한은 서버 tokenizer로 확인합니다.
5. **근거 / 파일 목록**: 파일 링크를 누르면 전체 원문과 선택한 청크가 표시됩니다. emoji도 Python 문자 offset에 맞춰 표시합니다.
6. **실행 결과 선택**: 이전 run도 조회할 수 있습니다. 제외 사유, 청크/벡터 개수, manifest의 모델 revision·필터·오류를 확인합니다.

답변의 인용 링크는 생성 입력에 실제 포함된 근거만 사용합니다. 잘못된 인용이나 생성 길이 제한은 화면에 표시합니다. 인용 ID 검사는 답변의 사실 정확성 검증과 다르므로 원문도 확인합니다.

## 개발과 검증

개발할 때는 backend와 Vite를 별도 터미널에서 실행합니다. Vite가 `/api`를 8000 포트로 프록시합니다.

```powershell
& $ragPython backend/run.py
# 별도 터미널
Set-Location frontend
npm run dev
```

```powershell
& $ragPython -m unittest discover -s backend/tests -p 'test_*.py' -v
& $ragPython backend/tests/check_service.py
& $ragPython backend/tests/check_service.py --models
```

```text
check_service.py: 실제 PostgreSQL에 migration/import/API/job/제약조건 검증
--models: 실제 E5 검색과 Qwen 생성까지 추가
--serve: --models와 함께 쓰면 임시 DB의 서버를 화면 검증용으로 유지. Ctrl+C 후 정리

임시 프로젝트와 volume만 정리하며 실제 서비스 DB는 건드리지 않음
모델 검증 결과: data/checks/model-result.json
서비스 요청 로그: data/logs/requests.jsonl
```

요청 로그에는 질문·생성 messages·결과가 로컬에 저장됩니다. `data/`는 Git에서 제외하며 비밀 환경변수 값은 제거합니다.

## 문제가 생겼을 때

| 증상 | 확인 |
| --- | --- |
| DB 비밀번호 오류 | 루트 .env를 채웠는지 확인. 기존 volume의 비밀번호는 .env 변경만으로 바뀌지 않음 |
| 테이블 없음 | manage.py migrate 실행 |
| 모델 캐시 없음 | manage.py download-models 또는 --encoder-only 실행 |
| CUDA 사용 불가 | auto 또는 --device cpu. CPU 생성은 RAM과 시간이 더 필요 |
| GPU_OOM | 다른 모델 프로세스 종료 또는 CPU 선택. 장치 auto는 여유 VRAM을 보장하지 않음 |
| 409 BUSY | 수집 배치 / 다른 추론 완료 후 재시도 |
| 질문 너무 김 | 500 token 이하로 줄이기. 자동 truncation 없음 |
| 화면이 없음 | npm run build 후 서버 재시작. HTML 직접 열기 대신 서버 주소 사용 |
| 서비스 중복 시작 | 기존 서버 종료. job runner는 DB advisory lock으로 중복 실행 거부 |

[[docs/project/validation|구현 검증 기록]]에서 실제 확인 범위와 남은 제한을 봅니다.
