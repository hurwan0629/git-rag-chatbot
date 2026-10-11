# 저장된 저장소 검색·답변 생성 테스트

기존 `tests/repo_ingestion/output/`을 읽어 저장소별 최신 완료 run에서 검색합니다. 문서 벡터는 재생성하지 않습니다. 모델·API·저장 스키마는 [계획서](../../docs/tests/retrieval-generation/plan.md)에 있습니다.

## 실행

프로젝트 루트의 PowerShell에서 실행합니다. 기존 수집 테스트와 같은 venv와 설치된 `torch`, `transformers`, `numpy`, `huggingface-hub`를 사용합니다.

```powershell
# 검색만: Qwen 로딩 없음
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' tests/retrieval_generation/server.py --search-only

# 검색과 답변 생성
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' tests/retrieval_generation/server.py
```

`준비 완료` 메시지가 나오면 Chrome에서 **http://127.0.0.1:8000/** 을 엽니다. 저장소를 체크하고 질문을 입력한 뒤 검색 또는 답변 생성을 누릅니다. 파일·청크·답변의 `[S1]` 링크를 누르면 원문 구간을 볼 수 있습니다.

```text
usage: server.py [-h] [--port PORT] [--search-only]

-h, --help      사용 방법
--port PORT     서버 포트, 기본 8000 (1~65535)
--search-only   검색만 실행, Qwen 다운로드·로딩 생략
```

종료는 `Ctrl+C`입니다. 포트 충돌 시 `--port 8001`로 실행하고 접속 주소도 바꿉니다. 새 수집 결과는 서버를 재시작하면 반영됩니다.

## 모델과 제한

- E5 질문 encoder는 저장된 벡터와 같은 revision을 사용합니다. 본문 최대 500 tokens, `query: `와 special tokens를 포함한 입력은 최대 512 tokens입니다. prefix는 서버가 붙입니다.
- 답변은 Qwen3 4B Instruct 2507을 CUDA/BF16으로 실행합니다. 최초 다운로드는 약 8 GB의 가중치와 추가 캐시 공간이 필요합니다. 확인된 revision은 `.model-cache/model.json`에 고정됩니다.
- 생성 입력은 Qwen 기준 최대 3584 tokens, 출력은 최대 512 tokens입니다. 낮은 순위 근거를 통째로 제외해 예산을 맞춥니다. 출력 길이 제한에 도달하면 화면에 표시합니다.
- Top K는 파일 수가 아닌 청크 수입니다. 점수는 확률이 아니며, 유효한 인용 ID도 답변 내용의 정확성을 보장하지 않습니다.
- 최신 완료 run이 손상되거나 임베딩 설정이 다르면 해당 저장소는 선택 불가입니다. 이전 run으로 자동 대체하지 않습니다. 수집 당시 제외된 파일은 검색되지 않습니다.

## 결과 저장

```text
tests/retrieval_generation/output/<session_id>/
├─ session.json    # 선택 run·모델 revision·실행 설정
└─ requests.jsonl  # 질문·검색 결과·답변·실제 생성 프롬프트·오류
```

기존 수집 데이터와 수집용 HTML은 그대로 유지합니다. 검색 화면은 서버 주소에서 여세요. `web/index.html`을 파일로 직접 여는 방식은 API를 호출할 수 없습니다.

## 검증

```powershell
# 외부 호출·모델 다운로드 없는 Python 단위 테스트
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' -m unittest discover -s tests/retrieval_generation -p 'test_*.py' -v

# Node가 있으면 HTML 스크립트 동작 확인
node --test tests/retrieval_generation/test_ui.mjs

# 서버 실행 후 실제 모델 검색 평가
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' tests/retrieval_generation/evaluate.py

# 답변 평가: 일반 모드 서버 필요
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' tests/retrieval_generation/evaluate.py --answer
```

평가 명령도 `--port`를 지원합니다. `fixtures/questions.json`의 질문과 기대 파일을 사용하고 `output/evaluation_<id>.json`에 기록합니다. 자동 기준은 근거 있는 질문의 Hit@5 ≥4/5와 API 오류 없음입니다. 답변의 사실성·인용 내용 일치는 별도로 검토해야 합니다.
