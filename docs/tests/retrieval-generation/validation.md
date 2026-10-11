---
title: 검색·답변 생성 테스트 검증 기록
status: tested
date: 2026-10-10
tags: [git-rag, retrieval, generation, validation]
---

# 검색·답변 생성 테스트 검증 기록

[[docs/tests/retrieval-generation/plan|구현 기준]] · [[docs/tests/retrieval-generation/review|리뷰]]

## 환경과 입력

지정한 `C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe`로 실행했습니다. Python 3.12.10, torch 2.14.0+cu130, transformers 5.17.0, numpy 2.4.6, huggingface-hub 1.32.0입니다. 새 패키지는 설치하지 않았습니다.

| 항목 | 실제 값 |
| --- | --- |
| GPU | NVIDIA GeForce RTX 5070 Ti Laptop, CUDA 사용 |
| E5 revision | `614241f622f53c4eeff9890bdc4f31cfecc418b3` |
| Qwen revision | `cdbee75f17c01a7cc42f958dc650907174af0554` |
| 입력 저장소 | `hurwan0629/runstop`: 447문서·1193벡터, `octocat/hello-world`: 1문서·1벡터 |
| 입력 데이터 검증 | 원문 hash·raw 일치·청크 전체 구간·ID·NPY 차원/정규화 통과 |
| Qwen 준비 | 가중치 3개 다운로드 약 142초, 콘솔 가중치 로딩 약 10초. 전체 시작 시간을 별도로 계측한 값은 아님 |
| GPU 메모리 관측 | `nvidia-smi`에서 전체 GPU 사용량 9234~9626 MiB / 12227 MiB. 다른 프로세스 포함, 프로세스별 최고 메모리는 미측정 |

## 자동 기능 검증

| 검증 | 결과 |
| --- | --- |
| 새 Python 단위·로컬 HTTP 테스트 | 26개 통과 |
| 실제 HTML 스크립트 동작 테스트 | Node 4개 통과 |
| 기존 수집 회귀 테스트 | 17개 통과 |
| CLI | help·검색 전용·일반 모드 실행 확인. 포트 충돌 시 모델 로딩 전에 종료 |
| HTTP 오류 | 잘못된 JSON/Unicode·16 KiB 초과·선택 오류·없는 문서·생성 비활성화·BUSY 확인 |
| 인용·화면 로직 | 없는/미전달 ID 검증, `[S1, S2]` 묶음 인용 연결, 이모지 문자 구간 강조 확인 |

Windows 임시 폴더와 Node 자식 프로세스 제한으로 샌드박스 실행이 실패해 승인받은 실행으로 재검증했습니다. 실패를 제외하고 통과한 최종 결과를 위에 기록했습니다. Chrome 실제 화면의 배치·클릭은 브라우저 연결이 없어 확인하지 못했고, HTML 스크립트는 DOM 대체 환경에서 실행했습니다.

## 실제 검색 평가

`fixtures/questions.json`의 근거 있는 질문 5개 + 근거 없는 질문 1개를 사용했습니다. 질문과 기대 경로는 결과를 보기 전에 실제 저장 원문을 확인해 작성했습니다.

| 질문 | Top 5 기대 파일 |
| --- | --- |
| AI 데이터 생성·학습·저장 모델 테스트 순서 | 포함 |
| Known 사용자 Warm Test/Val 분할 | 포함 |
| 모델 registry 위치와 지원 모델 | 포함 |
| artifact 점수로 후보 Top K 선택 | 미포함: 설계·병합 로그가 검색됨 |
| DEM 고도로 그래프 경사 준비 | 포함 |

**Hit@5 = 4/5**, 첫 검색 기준을 통과했습니다. 이 값은 작은 고정 질문의 파일 적중률이며 전체 코드 검색 성능이나 답변 정확도가 아닙니다. 유료 구독 매출 질문은 관련성이 낮은 청크가 반환되어, 생성 모델이 확인 불가를 답하는지 별도로 평가했습니다.

검색 기록: `tests/retrieval_generation/output/evaluation_e0f6bb0c988b4730959bbdd8fa0c91d5.json`.

## 실제 답변 생성 평가

| 항목 | 최초 프롬프트 1.0 | 수정 프롬프트 1.1 |
| --- | --- | --- |
| HTTP 생성 요청 | 6개 모두 200 | 6개 모두 200 |
| 인용 ID | 전달한 근거 안의 ID | 전달한 근거 안의 ID, 묶음 형식도 확인 |
| 종료 | 2개 length, 4개 EOS | 6개 모두 EOS |
| 출력 tokens | 220~512 | 167~361 |
| 내용 | 테스트 설명 혼동·추가 설명 오류 | 혼동·일반화·설계/구현 상충이 남음 |

수정 후 입력은 1688~3575 tokens, 생성은 약 7.5~16.3초, 전체 요청은 약 7.5~17.0초였습니다. OOM은 없었습니다. 근거 없는 매출 질문에는 확인할 수 없다고 답했습니다.

**생성 기능은 실행되지만, 답변의 사실성 기준은 미통과입니다.** 데이터 생성의 routing-worker 호출 여부, registry의 공통 클래스 일반화, 예정 문서와 현재 AI 호출 여부 혼합, 고도 처리의 미확인 함수명 추가를 확인했습니다. `answer_status=generated`는 생성 완료 상태이며 내용이 검증되었다는 뜻이 아닙니다.

| 기록 | 위치 |
| --- | --- |
| 최초 생성 평가 | `tests/retrieval_generation/output/evaluation_5c4bf8f022734c169670318d1df698ad.json` |
| 프롬프트 1.1 생성 평가 | `tests/retrieval_generation/output/evaluation_ebe099cfb2cd4999838aebbe89543265.json` |
| 최초 질문·근거·실제 프롬프트 | `tests/retrieval_generation/output/eb6b9f41752c447c9dd33152020a65de/requests.jsonl` |
| 수정 후 질문·근거·실제 프롬프트 | `tests/retrieval_generation/output/9a291dbba3f04787bf9861a2799865dc/requests.jsonl` |

기존 수집 결과를 재수집하거나 수정하지 않았습니다. 학습·추가 모델 선택·검색 전략 변경은 수행하지 않았으며 후속 개선점은 리뷰에 기록했습니다.
