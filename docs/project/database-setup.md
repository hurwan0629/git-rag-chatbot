---
title: Git RAG DB 실행과 환경 설정
status: validated
version: "0.1"
date: 2026-10-10
tags: [git-rag, database, docker, setup]
---

# DB 실행과 환경 설정

[[docs/project/index|서비스 설계 시작]] · [[docs/project/database|ERD와 제약조건]]

> [!info] 현재 범위
> Compose로 PostgreSQL 17과 pgvector 0.8.7을 준비하고 첫 DB 생성 시 vector 확장을 활성화합니다. ERD의 서비스 테이블은 backend의 Alembic migration 단계에서 만듭니다. 현재 수집·검색 테스트는 파일 저장을 계속 사용합니다.

## 목차

- [[#파일 구성|파일 구성]]
- [[#채울 환경변수|채울 환경변수]]
- [[#실행 방법|실행 방법]]
- [[#연결과 데이터 보존|연결과 데이터 보존]]
- [[#검증 기록|검증 기록]]
- [[#다음 검토 사항|다음 검토 사항]]

## 파일 구성

```text
git-rag/
├─ compose.yaml                   # DB 서비스와 영구 volume
├─ .env                           # 실제 설정, Git 제외
├─ .env.example                   # 공유용 설정 양식, 비밀 값 없음
└─ docker/postgres/init/
   └─ 001-vector.sql              # 최초 DB 생성 때 vector 확장 활성화
```

설정은 루트 `.env` 하나입니다. 별도 `.env.docker`는 사용하지 않습니다. Compose는 프로젝트 루트의 `.env`를 읽고, DB 컨테이너에는 DB 이름·계정·비밀번호만 전달합니다. GitHub token을 전달하지 않습니다. [Compose 환경변수 규칙](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)

## 채울 환경변수

| 변수 | 기본값 | 할 일 |
| --- | --- | --- |
| COMPOSE_PROJECT_NAME | git-rag | 프로젝트·volume 이름 기준. 보통 유지 |
| POSTGRES_HOST | 127.0.0.1 | 호스트에서 실행할 backend 연결 주소 |
| POSTGRES_PORT | 5432 | 이미 사용 중이면 5433 등으로 변경 |
| POSTGRES_DB | git_rag | 사용할 DB 이름 |
| POSTGRES_USER | git_rag | 최초 생성할 관리 계정 이름 |
| POSTGRES_PASSWORD | 빈 값 | **첫 실행 전에 반드시 채우기** |
| GITHUG_TOKEN | 빈 값 또는 기존 토큰 | GitHub 수집용. 기존 테스트 값이 있으면 실제 `.env`로 보존 |

```dotenv
# 값에 $ 또는 #가 있으면 단일 따옴표를 사용합니다.
POSTGRES_PASSWORD='직접-정한-비밀번호'
```

비밀번호가 비어 있으면 Compose가 시작 전에 오류를 냅니다. `.env.example`에는 실제 비밀번호·토큰을 넣지 않습니다.

## 실행 방법

Docker Desktop을 Linux containers 모드로 실행한 뒤 프로젝트 루트에서 사용합니다. 현재 PC에는 `.env`를 만들어두었으므로 비밀번호를 채우면 됩니다.

```powershell
# 설정 검증. --quiet를 사용해 비밀번호가 포함된 설정 출력을 피합니다.
docker compose config --quiet

# DB 시작 후 준비 상태까지 대기합니다.
docker compose up -d --wait db

# 상태와 로그
docker compose ps
docker compose logs --tail 50 db

# 실제 DB와 vector 확장 확인. 계정·DB 이름을 바꿨다면 -U / -d도 바꿉니다.
docker compose exec db psql -U git_rag -d git_rag -c "SELECT current_database(), extname, extversion FROM pg_extension WHERE extname = 'vector';"

# 종료. 이름 있는 DB volume은 유지됩니다.
docker compose down
```

아래처럼 대화형으로 확인해도 됩니다. 계정·DB 이름을 변경했다면 해당 인자도 바꿉니다.

```powershell
docker compose exec db psql -U git_rag -d git_rag
```

```sql
SELECT current_database(), extname, extversion
FROM pg_extension
WHERE extname = 'vector';
```

새 checkout에서 `.env`가 없다면 아래 명령으로 양식을 복사한 뒤 값을 채웁니다. 기존 `.env`는 덮어쓰지 않습니다.

```powershell
if (-not (Test-Path -LiteralPath '.env')) {
    Copy-Item -LiteralPath '.env.example' -Destination '.env'
}
```

## 연결과 데이터 보존

| 실행 위치 | DB 접속 |
| --- | --- |
| 현재 PC의 Python backend / DB 도구 | 127.0.0.1, POSTGRES_PORT, 지정 DB·계정·비밀번호 |
| 추후 같은 Compose 안의 backend | hostname `db`, port `5432`. backend의 POSTGRES_HOST만 `db`로 지정 |

`POSTGRES_HOST`는 DB 접속 클라이언트의 설정입니다. Compose의 외부 공개 주소는 현재 PC의 `127.0.0.1`로 고정합니다. host에서 바꾼 POSTGRES_PORT와 컨테이너 내부의5432는 구분합니다.

데이터는 프로젝트 이름을 포함한 `postgres_data` 이름 있는 volume에 저장됩니다. 컨테이너를 다시 만들거나 `down`해도 유지됩니다. 프로젝트 이름을 바꾸면 다른 volume을 사용하게 됩니다.

첫 초기화가 끝난 뒤 `.env`의 DB 이름·계정·비밀번호만 바꿔도 기존 DB가 변경되지는 않습니다. 변경은 SQL과 계정 관리 작업으로 수행해야 합니다. init SQL도 빈 데이터 디렉터리의 첫 시작에만 실행되며, 이후 구조 변경은 migration으로 관리합니다. [PostgreSQL 이미지 초기화 규칙](https://hub.docker.com/_/postgres)

이미지는 `pgvector/pgvector:0.8.7-pg17-bookworm`으로 고정했습니다. 해당 태그와 Docker 사용 방식은 [pgvector 공식 문서](https://github.com/pgvector/pgvector#docker)를 기준으로 합니다.

## 검증 기록

2026-10-10, Docker Engine 28.3.2 / Compose 2.39.1에서 지정 venv로 `tests/infrastructure/check_database.py`를 실행했습니다. 실제 Compose 파일과 `.env.example`을 사용하되 별도 프로젝트 이름·임시 비밀번호·자동 할당 로컬 포트로 격리했습니다.

| 검증 | 결과 |
| --- | --- |
| 빈 비밀번호 시작 전 거부 | 통과 |
| DB 설정만 컨테이너에 전달, localhost 바인딩, init SQL 읽기 전용 | 통과 |
| PostgreSQL 17 healthy, vector 확장0.8.7 자동 생성 | 통과 |
| TCP 비밀번호 인증, 잘못된 비밀번호 거부, `$`·`#` 문자 보존 | 통과 |
| vector(384) 저장과 cosine 검색 순위 | 통과 |
| down 후 up으로 컨테이너 재생성 시 데이터 보존 | 통과 |
| 검증용 컨테이너·volume 정리 | 통과 |

루트 `.env` 생성·기존 GitHub token 보존·Git 제외, 문서 링크·JSON 예시·검증 스크립트 구문도 확인했습니다. 실제 프로젝트 DB는 초기화하지 않았습니다. 사용자가 `.env` 비밀번호를 채운 뒤 시작합니다. 장치 자동 선택은 서비스 설계에 반영했으며 CPU에서 Qwen 생성하는 실제 모델 검증은 아직 수행하지 않았습니다.

## 다음 검토 사항

로컬 MVP로 확정했습니다. user 테이블·로그인·사용자별 사용량 제한은 추가하지 않습니다. 현재 PC에서 실행하되 clone한 다른 PC에서도 CUDA 사용 가능 여부에 따라 GPU 또는 CPU를 선택하도록 서비스 모델 로딩을 구현합니다.

남은 것은 ERD migration, 기존 파일 import, 수집·검색의 DB 연결입니다. 입력의 형식·token 예산 검증과 모델 실행 직렬화는 유지합니다.

검증 스크립트는 새 프로젝트 이름과 임시 비밀번호로 실제 Compose를 실행합니다. vector(384) 검색과 컨테이너 재생성 후 데이터 보존을 확인한 뒤 **자신이 생성한 검증 프로젝트의 volume만 정리**합니다. 실제 `.env` 비밀번호나 프로젝트 DB는 사용하지 않습니다.

```powershell
python tests/infrastructure/check_database.py
```
