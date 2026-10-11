---
title: Git RAG 테스트 설계 목록
status: draft
date: 2026-10-10
tags:
  - git-rag
  - testing
aliases:
  - 테스트 설계 목록
---

# Git RAG 테스트 설계 목록

전체 방향은 [[git-rag-design|Git RAG 초기 설계]]에서 확인합니다. 이 폴더에는 개별 테스트의 계획과 확인할 결과를 기록합니다.

| 테스트 | 계획서 | 현재 상태 |
| --- | --- | --- |
| 저장소 수집·청킹·임베딩·HTML 조회 | [[docs/tests/repo-ingestion/plan\|설계 배경]] · [[docs/tests/repo-ingestion/final-design\|최종 설계·스키마]] · [[docs/tests/repo-ingestion/usage\|명령어·사용 방법]] · [[docs/tests/repo-ingestion/validation\|검증 기록]] · [[docs/tests/repo-ingestion/review\|테스트 후 리뷰]] | 구현 완료, 실제 수집·임베딩 실행 확인 |
| 저장 데이터 검색·답변 생성 | [[docs/tests/retrieval-generation/plan\|계획·스키마·사용 방법]] · [[docs/tests/retrieval-generation/validation\|검증 기록]] · [[docs/tests/retrieval-generation/review\|리뷰]] | 구현 완료, 검색·생성 검증 기록 참고 |

> [!info] 문서와 실행 코드의 위치
> `docs/tests/`는 테스트 설계 문서용입니다. 실행 코드는 프로젝트 루트의 `tests/`에 둡니다.
