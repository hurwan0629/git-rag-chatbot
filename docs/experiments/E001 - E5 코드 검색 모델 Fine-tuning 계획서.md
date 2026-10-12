# E5 코드 검색 모델 Fine-tuning 계획서

## 1. 프로젝트 목표

GitHub 저장소에서 자연어 질문과 관련된 코드·문서를 더 정확하게 검색하도록 **multilingual-e5-small 모델을 Fine-tuning**한다.

기존 RAG 파이프라인은 유지하고, 검색 모델의 가중치만 변경하여 학습 전후 성능을 비교한다.

## 2. 모델 및 데이터셋

|구분|내용|
|---|---|
|모델|intfloat/multilingual-e5-small (약 118M)|
|학습 데이터|CodeSearchNet 20,000쌍|
|평가 데이터|Long Code Arena – Bug Localization|
|학습 방식|Contrastive Learning (InfoNCE)|
|학습 환경|RTX 5070 Laptop GPU 12GB|
|평가 지표|Recall@K, MRR@K, nDCG@K|

CodeSearchNet은 자연어와 코드의 의미적 관계 학습에 사용하고, Long Code Arena는 실제 저장소에서 관련 파일을 검색하는 능력을 평가하는 데 사용한다.

## 3. 실험 진행

**1단계 — Baseline 측정**

- 원본 E5로 평가 데이터셋 검색
    
- Recall@5, MRR@10, nDCG@10 기록
    

**2단계 — Fine-tuning**

- CodeSearchNet에서 20,000개 Query–Positive 쌍 추출
    
- InfoNCE 기반 대조학습 수행
    
- Validation 결과로 최적 체크포인트 선정
    

**3단계 — 성능 비교**

- Fine-tuned E5로 동일한 평가셋 재검색
    
- 학습 전후 지표와 검색 실패 사례 비교
    
- 정답 파일을 찾지 못하는 원인 분석
    

## 4. 실험 원칙

- 학습·검증·테스트 데이터 분리 및 중복 방지
    
- Chunking, 검색 방식, Top-K 등 평가 조건 고정
    
- Qwen과 Generation 파이프라인은 변경하지 않음
    
- 기존 모델과 학습 모델의 가중치·실험 설정·결과 별도 저장
    

## 5. 최종 결과물

1. Fine-tuned E5 모델 체크포인트
    
2. 학습 Loss 및 Validation 기록
    
3. 원본 모델 대비 Retrieval 성능 비교표
    
4. 검색 실패 사례 및 개선 결과 분석
    

**성공 기준:** 동일한 평가 조건에서 원본 E5보다 Recall@K, MRR@K 등의 검색 지표가 개선되는지 확인하고, 실제 Git RAG에 적용할 가치가 있는지 판단한다.