---
title: 저장소 수집 테스트 구현 검증 기록
status: verified
date: 2026-10-10
tags:
  - git-rag
  - testing
  - validation
---

# 저장소 수집 테스트 구현 검증 기록

[[docs/tests/repo-ingestion/final-design|최종 설계]] · [[docs/tests/repo-ingestion/usage|명령어와 사용 방법]] · [[docs/tests/index|테스트 목록]]

## 구현 결과

`tests/repo_ingestion/run.py` 하나로 GitHub 조회→청킹→임베딩→파일 저장→검증→HTML 생성까지 실행합니다. HTML은 `tests/repo_ingestion/output/index.html` 하나이며 repo/run 선택·원문·청크 링크를 제공합니다.

실행기는 지정한 `C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe`를 사용했습니다. 설치나 기존 단일 파일 테스트 수정은 하지 않았습니다. 실행 중 tokenizer·encoder는 프로젝트 내부 `.model-cache/`에 다운로드했습니다.

## 자동 테스트

```powershell
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' -m unittest discover -s tests/repo_ingestion -p 'test_*.py' -v
```

17개 테스트 통과. 테스트에서는 네트워크를 호출하지 않습니다. 최초 구현의 16개에 코드·설정 확장자 기본값과 명시적 override 검증을 추가했습니다.

| 확인 대상 | 결과 |
| --- | --- |
| URL·상대 경로·환경 변수 alias 및 덮어쓰기 규칙 | 통과 |
| 파일 필터·자격 증명 제외·실행 가능한 텍스트 허용 | 통과 |
| 잘린 tree 순회 및 목록 불완전 처리 | 통과 |
| blob SHA·size·Base64 응답 검증 | 통과 |
| UTF-8·BOM·이모지·CRLF 보존, 바이너리·인코딩 제외 | 통과 |
| 인증/호출 제한 오류 구분·토큰 비출력·미처리 파일 상태 기록 | 통과 |
| token 예산·긴 한 줄·전체 원문 구간 보존 | 통과 |
| padding 제외 mean pooling, 실패 batch와 vector_row 연결 | 통과 |
| 전체 파이프라인·저장 재로드·변조된 벡터 참조 거부 | 통과 |
| collect-only·여러 run 집계·안전한 JSON HTML 삽입 | 통과 |
| 모델 실패 시 원문 보존·partial 상태·잘못된 이전 run 처리 | 통과 |

Windows 샌드박스의 임시 폴더 접근 제한으로 첫 테스트 실행이 실패했고, 승인된 제한 밖 실행에서 통과했습니다.

## 실제 GitHub·E5 실행

입력 repo: `https://github.com/octocat/Hello-World`

```powershell
& 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe' tests/repo_ingestion/run.py https://github.com/octocat/Hello-World --env-file tests/git_fetch/.env
```

| 항목 | 결과 |
| --- | --- |
| run_id | `20261010T084748Z_d57edd873ce24c6c99261a47621df98b` |
| 실행 상태 | `completed`, 종료 코드 0 |
| 수집·문서·청크·벡터 | 각각 1개 |
| 임베딩 모델 | `intfloat/multilingual-e5-small` |
| tokenizer·encoder revision | `614241f622f53c4eeff9890bdc4f31cfecc418b3` |
| 실제 추론 장치 | `cuda` |
| NPY | shape `(1, 384)`, dtype `float32`, norm `1.0` |
| 저장 결과 | manifest·CSV·JSONL·NPY·원문 TXT·집계 HTML 생성 및 재로드 검증 성공 |

샌드박스 네트워크 제한으로 실패한 첫 run도 별도 폴더에 남았습니다. 승인된 네트워크 접근으로 재실행한 run은 성공했습니다. 이전 실패 run과 성공 run을 같은 HTML에서 구분해 표시합니다.

실제 E5 tokenizer/encoder에 한글·이모지·CRLF·긴 한 줄을 포함한 로컬 예제를 추가 입력했습니다. 원문 전체 복원과 최종 입력 512 tokens 이하를 확인했고, 12개 청크를 `(12, 384)` float32 벡터로 변환했습니다. 모든 값이 유한하고 정규화 검증을 통과했습니다. 이 예제는 검증만 수행하고 repo run으로 저장하지 않았습니다.

## HTML 확인 범위

생성 HTML의 JavaScript를 Node로 실행하는 간단한 DOM 테스트에서 문법, 기본 run 선택, 파일 링크 이동, 청크 상세, `vector_row: 0` 표시를 확인했습니다. 원문에 `</script>`·HTML 태그가 있어도 JSON 데이터가 script를 닫지 않도록 변환되는 것을 자동 테스트로 확인했습니다. 이모지 위치는 Python 문자 offset에 맞게 JavaScript에서 `Array.from()`으로 처리합니다.

현재 UI 도구에 연결된 브라우저가 없어 실제 브라우저 화면·레이아웃·클릭 조작은 확인하지 못했습니다. `output/index.html`을 브라우저로 열어 시각적으로 확인할 수 있습니다.

대규모 repo, GitHub 실제 rate limit, 검색 품질, 모델 학습은 이번 검증 범위에 포함하지 않았습니다.

## 테스트 후 리뷰

[[docs/tests/repo-ingestion/review|테스트 후 리뷰·서비스 개선 사항]]에 단계 내부의 파일 수집·모델 준비·추론 진행률을 `%`로 표시하는 후속 요구사항을 기록했습니다.

## runstop 결과와 수집 기준 확장 확인

사용자 실행의 `hurwan0629/runstop` 결과는 원문 447개, 청크·벡터 각각 1,193개였습니다. NPY를 재로드해 `(1193, 384)` float32, 모든 값 유한, L2 norm 범위 약 `0.99999988~1.00000012`를 확인했습니다. 이 검사는 벡터 생성·수치 무결성을 확인하며 검색 정확도를 평가한 것은 아닙니다.

기존 `.py/.md/.txt` 정책에서 제외된 항목을 새 코드·설정 파일 정책으로 재평가하면 metadata 기준 494개가 추가 대상이며, 이 중 `.ts/.tsx`는 270개입니다. 아직 본문을 새로 수집한 결과가 아니므로 UTF-8·크기 등의 최종 검증에서 일부 제외될 수 있습니다. 기존 run은 보존하며 새 기준은 재실행할 때 적용됩니다.
