---
title: Git RAG 서비스 구현 검증
status: validated
date: 2026-10-10
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

단위 테스트 합계는 **57개**입니다. DB 통합과 실제 모델·브라우저 검증은 별도로 수행했습니다.

## 실제 저장 데이터

기존 output 4개 run은 원본을 유지하고 서비스 DB로 옮겼습니다. import 당시 활성 데이터는 다음과 같습니다.

| repo | 활성 원문 | 활성 청크 / 벡터 |
| --- | --- | --- |
| hurwan0629/runstop | 941 | 5859 / 5859 |
| octocat/hello-world | 1 | 1 / 1 |

과거 run도 DB에 남습니다. 검색은 전체 과거 run을 섞지 않고 repo의 활성 run만 사용합니다. 검증 중 네트워크 제한으로 실패한 재수집도 오류 기록으로 보존했습니다. 이 실패는 기존 검색 데이터를 바꾸지 않았으며 네트워크 접근 가능한 서버에서 재수집이 완료됐습니다.

## 검증 명령

```powershell
$ragPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $ragPython -m unittest discover -s backend/tests -p 'test_*.py' -v
& $ragPython backend/tests/check_service.py --models
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
