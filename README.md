# Git RAG · 로컬 MVP

GitHub 저장소를 수집하고, 선택한 저장소의 코드·문서에서 근거를 찾아 답변합니다.
FastAPI + React + PostgreSQL/pgvector를 사용합니다. E5는 검색 벡터를 만들고 Qwen은 답변을 생성합니다.

자세한 실행 방법은 [사용 안내](docs/project/usage.md), 구조와 ERD는 [서비스 문서](docs/project/index.md)를 확인하세요. Obsidian에서도 읽을 수 있습니다.

## 처음 실행

프로젝트 루트에서 실행합니다. `.env.example`을 `.env`로 복사하고 `POSTGRES_PASSWORD`를 채우세요. `GITHUG_TOKEN`은 GitHub 호출에 사용하며 비워 두면 공개 저장소를 인증 없이 읽습니다.

```powershell
# 현재 PC의 지정 가상환경
$ragPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $ragPython -m pip install -r backend/requirements.txt
docker compose up -d --wait db
& $ragPython backend/manage.py migrate

# 다른 PC에서 처음 실행할 때. 현재 PC는 기존 테스트 캐시도 재사용합니다.
& $ragPython backend/manage.py download-models

# 기존 테스트 결과를 옮길 때만 실행
& $ragPython backend/manage.py import-output

Push-Location frontend
npm ci
npm run build
Pop-Location
& $ragPython backend/run.py
```

브라우저에서 **http://127.0.0.1:8000**을 엽니다. API 문서는 **http://127.0.0.1:8000/docs**입니다. HTML 파일을 직접 열지 않습니다.

다른 PC는 Python 3.12 가상환경을 만들고 `$ragPython`을 해당 Python 경로로 바꾸세요. CUDA 지원 PyTorch 설치 방식은 [공식 설치 안내](https://pytorch.org/get-started/locally/)를 따릅니다. CUDA가 없으면 CPU를 선택합니다. CPU Qwen 실행에는 FP32 가중치 약 16 GB와 추가 메모리가 필요하며 시간이 더 걸립니다.

```powershell
& $ragPython backend/run.py --search-only   # E5 검색만 실행
& $ragPython backend/run.py --device cpu    # CPU로 명시적 실행
```

## 검증

```powershell
& $ragPython -m unittest discover -s backend/tests -p 'test_*.py' -v
& $ragPython backend/tests/check_service.py --models
Push-Location frontend
npm test
npm run build
Pop-Location
```

통합 검증은 임시 Compose 프로젝트·DB를 만들고 끝나면 해당 테스트 데이터만 제거합니다. 기존 DB와 `.env` 비밀번호를 사용하지 않습니다.

로컬 MVP에는 로그인·사용자 테이블·사용량 quota가 없습니다. 모델 실행은 한 프로세스에서 직렬화하며 요청이 겹치면 `409 BUSY`를 반환합니다. 생성 답변은 출처 원문과 함께 확인하세요.
