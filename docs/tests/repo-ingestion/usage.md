---
title: 저장소 수집 테스트 명령어와 사용 방법
status: implemented
phase: implementation
version: "1.0"
date: 2026-10-10
tags:
  - git-rag
  - cli
  - usage
aliases:
  - 저장소 수집 help
---

# 저장소 수집 테스트 명령어와 사용 방법

[[docs/tests/index|테스트 목록]] · [[docs/tests/repo-ingestion/final-design|최종 설계·스키마]] · [[docs/tests/repo-ingestion/plan|설계 배경]]

> [!important] 실행 가능한 CLI
> `tests/repo_ingestion/run.py`, `report.py`를 구현했습니다. 아래 명령어는 프로젝트 루트에서 지정 venv로 실행합니다. 기존 `tests/git_fetch/fetch_github.py`와 별도 테스트입니다. [[docs/tests/repo-ingestion/validation|구현 검증 기록]]도 확인할 수 있습니다.

## 1. run.py --help

```text
usage: run.py [-h] [--ref REF] [--scope PATH] [--env-file PATH]
              [--collect-only] [--device {auto,cpu,cuda}]
              [--batch-size N] [--max-file-bytes N]
              [--extensions LIST] REPO_URL

GitHub 저장소를 수집·청킹하고 임베딩을 파일로 저장합니다.
실행 후 모든 repo/run을 조회하는 output/index.html을 갱신합니다.

positional arguments:
  REPO_URL                 GitHub HTTPS repo 루트 URL (필수)

options:
  -h, --help               도움말 출력 후 종료; API·모델 호출 없음
  --ref REF                branch/tag/commit (기본: repo 기본 브랜치)
  --scope PATH             repo 내부 폴더 (기본: repo 전체)
  --env-file PATH          사용할 .env 경로 (기본: 프로젝트 루트부터 상위 탐색)
  --collect-only           원문·청크·HTML만 저장; encoder·NPY 생성 생략
  --device {auto,cpu,cuda}  추론 장치 (기본: auto)
  --batch-size N           임베딩 배치 크기, 양의 정수 (기본: 8)
  --max-file-bytes N       파일별 최대 byte 크기, 양의 정수 (기본: 1000000)
  --extensions LIST        쉼표로 구분한 허용 확장자 (기본: 코드·설정 파일 목록)

fixed settings:
  model                    intfloat/multilingual-e5-small
  max input tokens         512 (passage prefix + special tokens 포함)
  chunk overlap            0
  output                   tests/repo_ingestion/output/
  model cache              tests/repo_ingestion/.model-cache/
```

### 인자 입력 규칙

| 인자                 | 입력 예시                                    | 주의할 점                                                         |
| ------------------ | ---------------------------------------- | ------------------------------------------------------------- |
| `REPO_URL`         | `https://github.com/octocat/Hello-World` | `.git` 끝부분 허용. SSH URL·GitLab·`/tree/...` URL은 첫 버전에서 지원하지 않음 |
| `--ref`            | `main`, `v1.0`, commit SHA               | branch 이름에 `/`가 있어도 인자로 그대로 전달. 저장 시 실제 commit SHA 기록         |
| `--scope`          | `docs`, `src/utils`                      | repo 내부 폴더이며 로컬 폴더가 아님. 파일 단일 경로·절대 경로·`..`는 거부               |
| `--env-file`       | `tests/git_fetch/.env`                   | 상대 경로는 현재 터미널 위치 기준. 명시한 파일이 없으면 입력 오류                        |
| `--collect-only`   | 값 없는 flag                                | 청킹에 tokenizer가 필요해 최초 tokenizer 다운로드는 발생할 수 있음                |
| `--device`         | `cpu`, `cuda`, `auto`                    | 추론 시 명시한 cuda를 사용할 수 없으면 오류. auto는 CPU로 선택 가능                 |
| `--batch-size`     | `4`, `8`                                 | GPU 메모리가 부족하면 줄여서 새 run으로 실행                                  |
| `--max-file-bytes` | `1000000`, `2000000`                     | 토큰 수가 아닌 원문 byte 수 제한                                         |
| `--extensions`     | `".py,.md,.txt,.js,.ts"`                 | 점으로 시작하는 확장자, 공백 제거·소문자 정규화. 자격 증명 제외 규칙은 계속 적용               |

기본 목록은 `.py/.md/.txt` 외에 `.ts/.tsx/.js/.jsx/.mjs/.cjs`, `.json/.yaml/.yml/.toml`, `.html/.css`, `.java/.kt` 등을 포함합니다. 전체 목록은 `config.py`의 `DEFAULT_EXTENSIONS`에서 확인합니다. README·LICENSE·Dockerfile·Makefile과 `.gitignore/.dockerignore/.editorconfig`도 기본 포함합니다.

`--extensions`는 기본 목록에 추가하는 옵션이 아니라 기본 확장자 목록을 대체하는 옵션입니다. `.csv`를 지정하면 CSV를 행 구조로 분석하지 않고 전체 텍스트로 수집합니다. `.ipynb`는 셀 추출 기능이 없으므로 이번 테스트에서 추가하지 않는 것을 권장합니다. 새 수집 기준으로 원문과 벡터를 만들려면 새 run을 실행해야 합니다.

`--collect-only`와 함께 지정한 device·batch-size는 기록만 하고 추론에 사용하지 않습니다. 모델·청킹 방식·출력 경로를 변경하는 옵션은 첫 버전에 두지 않습니다.

## 2. PowerShell 실행 준비

터미널 위치를 프로젝트 루트 `git-rag`로 두고, 지정 venv 실행기를 변수에 넣습니다. 환경 활성화 없이도 해당 Python을 사용합니다.

```powershell
$gitRagPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
```

현재 `.env` 위치는 `tests/git_fetch/.env`이므로 아래 예제는 `--env-file`로 명시합니다. 내용은 다음 키 중 하나입니다. 실제 값을 문서나 명령어 인자로 넣지 않습니다.

```dotenv
GITHUG_TOKEN=여기에_본인_토큰
```

`GITHUG_TOKEN`을 먼저 읽고, 값이 없으면 `GITHUB_TOKEN`을 읽습니다. 이미 설정된 프로세스 환경 변수는 `.env`로 덮어쓰지 않습니다. 공개 repo는 토큰 없이 조회할 수 있으나 호출 제한의 영향을 받습니다. 다른 사람의 비공개 repo는 사용 계정의 접근 권한도 있어야 합니다.

## 3. 단계별 사용 방법

### 도움말 확인

```powershell
& $gitRagPython tests/repo_ingestion/run.py --help
& $gitRagPython tests/repo_ingestion/report.py --help
```

### 먼저 수집·청킹만 확인

```powershell
& $gitRagPython tests/repo_ingestion/run.py https://github.com/octocat/Hello-World --env-file tests/git_fetch/.env --collect-only
```

예상 결과: manifest·파일 목록·원문·청크·TXT·집계 HTML. `status=collected_only`, chunk의 `embedding_status=not_run`, `vector_row=null`, NPY 없음.

### 같은 repo를 임베딩까지 실행

```powershell
& $gitRagPython tests/repo_ingestion/run.py https://github.com/octocat/Hello-World --env-file tests/git_fetch/.env
```

새 run으로 수집부터 다시 실행하고 `vectors.npy`까지 저장합니다. 이전 collect-only run은 유지하며, HTML에서 두 run을 모두 비교할 수 있습니다. 첫 실행에는 encoder 다운로드 시간이 필요할 수 있습니다.

### 특정 폴더·ref·확장자를 지정

아래 `OWNER/REPO`, `main`, `docs`는 테스트할 repo에 맞춰 바꿉니다.

```powershell
& $gitRagPython tests/repo_ingestion/run.py https://github.com/OWNER/REPO --env-file tests/git_fetch/.env --ref main --scope docs --extensions ".py,.md,.txt,.js,.ts" --device cpu --batch-size 4 --max-file-bytes 1000000
```

모든 경로를 목록으로 기록하되 scope 밖 파일은 `outside_scope`로 제외합니다. 입력한 ref는 run 시작 시 commit 하나로 고정합니다.

### 기존 저장 결과로 HTML만 다시 생성

```text
usage: report.py [-h]

기존 output/의 모든 repo/run 파일을 읽어 index.html을 다시 만듭니다.
GitHub API 호출·tokenizer/model 로딩·벡터 재계산은 하지 않습니다.

options:
  -h, --help    도움말 출력 후 종료
```

```powershell
& $gitRagPython tests/repo_ingestion/report.py
```

### 결과 열기

파일 탐색기에서 `tests/repo_ingestion/output/index.html`을 브라우저로 엽니다. repo/run 선택 → 파일 목록 → 파일 링크 → 원문·청크 확인 순서로 조회합니다. 제외·실패 항목은 이유를 확인하고, 수집된 파일은 chunk 경계·token 수·벡터 상태를 확인합니다. 새 run 실행 후 페이지를 새로고침합니다.

## 4. 출력 경로와 종료 코드

```text
tests/repo_ingestion/output/
├─ index.html
└─ octocat--hello-world/
   ├─ RUN_ID_1/   # 수집·청킹만 수행한 run
   └─ RUN_ID_2/   # 임베딩까지 수행한 run
```

repo 폴더명은 정규화된 소문자 `owner--repo`입니다. 실제 run_id는 UTC 시각 + UUID이며 위 이름은 설명용입니다. run_id와 저장 경로, 수집·제외·실패·청크·벡터 개수를 콘솔에 표시합니다. 토큰은 일부 문자열도 출력하지 않습니다.

| 종료 코드 | 의미 |
| --- | --- |
| `0` | help 출력 또는 정상 완료. collect-only 정상 완료·규칙에 따른 제외 포함 |
| `1` | API·모델·저장·HTML 처리 실패. partial/failed 결과를 확인 |
| `2` | 잘못된 인자, URL, scope, 숫자 또는 존재하지 않는 명시적 env 파일 |

완료 여부는 종료 코드와 `manifest.json`의 status를 함께 확인합니다. 파일별 제외 이유는 `files.csv`, chunk별 실패 이유는 `chunks.jsonl`, 실행 오류는 manifest에 저장합니다.

## 5. 구현 후 확인 명령

```powershell
& $gitRagPython -m unittest discover -s tests/repo_ingestion -p 'test_*.py' -v
```

고정 예제로 UTF-8/바이너리 구분, 제외 기준, 512 token 한도, 원문 slice, vector 행 연결, 안전한 HTML 삽입을 검증합니다. 실제 GitHub 조회와 모델 다운로드·추론 결과는 [[docs/tests/repo-ingestion/validation|검증 기록]]에 남깁니다.
