---
title: Git RAG 서비스 구현 검증
status: validated
date: 2026-10-11
tags: [git-rag, validation, mvp]
---

# 서비스 구현 검증

[[docs/project/index|서비스 문서]] · [[docs/project/usage|실행 방법]]

## 확인 결과

| 범위 | 결과 |
| --- | --- |
| backend 단위 테스트 | 7개 통과. 입력·JSON 경계·token 예산·인용·장치/dtype·포트 충돌 |
| frontend 유틸리티 테스트 | 3개 통과. emoji offset·GitHub 링크·진행률 |
| 기존 수집 테스트 | 17개 통과 |
| 기존 검색·생성 테스트 | 26개 통과 |
| 기존 HTML UI 테스트 | 4개 통과 |
| TypeScript / Vite | 빌드 통과 |
| 실제 PostgreSQL 17 + pgvector 0.8.7 | Alembic 반복 적용, 테이블·복합 FK·CHECK 검증 통과 |
| 기존 output import | 4개 run 저장, 재import skip, 최신 완료 run 활성화 |
| DB/API 통합 | 목록·manifest·pagination·제외 사유·원문 역참조·BUSY·token 제한 통과 |
| 수집 실행기 | background 실행·진행도·중복 차단·활성 전환·실패 시 이전 run 유지 통과 |
| 재시작 | running job→interrupted, run→failed 확인 |
| 실제 CUDA 모델 | E5 검색 + Qwen BF16 생성 통과. 입력 예산과 인용 범위 확인 |
| 실제 CPU 모델 | E5의 384차원 float32·유한값·L2 정규화 확인 |
| Chrome | 등록+수집·완료 진행률·run 파일 목록·저장소 선택·답변·근거·원문 dialog·선택 청크 강조 표시 확인 |
| 실제 GitHub 재수집 | octocat/hello-world 원문 1개·청크 1개·벡터 1개 완료, DB 활성화 확인 |

최초 구현 검증 당시 단위 테스트 합계는 **57개**입니다. DB 통합과 실제 모델·브라우저 검증은 별도로 수행했습니다. 2026-10-11 변경 검증은 아래에 별도로 기록합니다.

## 실제 저장 데이터

기존 output 4개 run은 원본을 유지하고 서비스 DB로 옮겼습니다. import 당시 활성 데이터는 다음과 같습니다.

| repo | 활성 원문 | 활성 청크 / 벡터 |
| --- | --- | --- |
| hurwan0629/runstop | 941 | 5859 / 5859 |
| octocat/hello-world | 1 | 1 / 1 |

과거 run도 DB에 남습니다. 검색은 전체 과거 run을 섞지 않고 repo의 활성 run만 사용합니다. 검증 중 네트워크 제한으로 실패한 재수집도 오류 기록으로 보존했습니다. 이 실패는 기존 검색 데이터를 바꾸지 않았으며 네트워크 접근 가능한 서버에서 재수집이 완료됐습니다.

## partial 검색 허용 검증 — 2026-10-11

- 서버의 활성화·목록·manifest·검색 조건에 `partial`을 포함했습니다. 모델 호환성, 벡터 1개 이상, 원문·청크·벡터 무결성 검증은 유지합니다.
- backend 단위 테스트 8개, frontend 유틸리티 테스트 3개와 TypeScript/Vite 빌드가 통과했습니다.
- 격리된 실제 PostgreSQL 통합 테스트에서 다운로드 일부 실패와 벡터화 배치 일부 실패를 확인했습니다. 청크 9개 / 벡터 1개인 결과도 검색·답변 가능하며, 실패 청크는 검색에서 제외됩니다.
- 벡터 0개 결과의 활성화 거부, 기존 저장 결과 활성화, 저장 데이터 검증 실패 시 활성 연결 rollback을 확인했습니다. 임시 DB는 정리했고 실제 DB 데이터는 보존했습니다.
- 기존 `openai/openai-python`의 run `20261011T043536Z_bbf7919d70854185996253575785bb4e`를 실제 E5 tokenizer로 재검증한 뒤 활성화했습니다. 상태는 `partial`, 문서 2,373개 / 청크 8,930개 / 벡터 8,930개입니다.
- 실패 파일 1개는 `src/openai/resources/admin/organization/projects/__init__.py`이며 이유는 `network_error`입니다. 실패 및 이전 중단 run의 기록은 유지합니다.

실제 CUDA E5와 HTTP API로 다음 세 질문을 검색했습니다. 각 질문은 Top 5에 예상 근거가 있었고, 반환된 15개 결과 모두 선택 저장소·활성 run·embedded 상태 및 전체 원문의 문자 구간과 일치했습니다.

| 질문 | 최상위 파일 | cosine |
| --- | --- | --- |
| AsyncOpenAI 비동기 요청 사용법 — 한국어 질문 | examples/async_demo.py | 0.8829 |
| max_retries와 timeout 설정 | README.md | 0.9349 |
| APIConnectionError와 RateLimitError 처리 | README.md | 0.9111 |

전체 결과는 로컬 `data/checks/saved-search-result.json`에 저장합니다. 이 세 사례는 기능 확인이며 전체 검색 품질 평가나 새로운 실제 Qwen 답변 품질 검증은 아닙니다.

## 검증 명령

```powershell
$ragPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $ragPython -m unittest discover -s backend/tests -p 'test_*.py' -v
& $ragPython backend/tests/check_service.py --models
& $ragPython backend/tests/check_saved_search.py https://github.com/openai/openai-python
```

```powershell
Push-Location frontend
npm test
npm run build
Pop-Location
```

통합 테스트는 임시 Compose 프로젝트를 생성하고 종료 시 해당 volume을 정리합니다. 실제 `git-rag_postgres_data` volume은 유지합니다. 모델 검증을 실행할 때는 다른 Qwen 서버를 종료해 중복 GPU 로딩을 피합니다.

## 남은 제한

- Qwen CPU FP32 로딩·생성 전체는 이 PC에서 실행하지 않았습니다. CPU 장치 선택·FP32 분기는 단위 테스트, 실제 CPU 인코딩은 E5로 검증했습니다.
- 실제 생성 답변 1건의 기능 동작을 확인했으며 답변 전체의 사실 정확성이나 검색 품질을 보장하는 평가가 아닙니다. 현재 모델과 prompt를 유지하고 출처 원문으로 확인합니다.
- 수집 진행률은 현재 단계 기준입니다. 답변 생성은 경과 시간을 표시하며 token streaming은 구현하지 않았습니다.
- 로그인·quota·삭제 API·학습·HNSW·외부 job queue는 이번 로컬 MVP 범위에 포함하지 않았습니다.
