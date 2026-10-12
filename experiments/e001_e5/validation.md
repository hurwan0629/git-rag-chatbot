# E001 구현 검증 · 2026-10-11

[실험 안내](README.md) · [분석 노트북](analysis.ipynb)

지정한 venv의 실제 E5를 사용해 CPU에서 검증했습니다. 아래 결과는 구현 동작 확인이며 정식 성능 비교가 아닙니다.

## 확인한 내용

| 검증 | 결과 |
| --- | --- |
| 단위 테스트 10개 | 통과: 다중 정답 지표, 파일 최대 점수 집계, docstring 제거, query 길이, 중복/저장소 분리, 파일 hash, ZIP 경로/분할/GZ, LCA datetime·누락 정답, 독립 체크포인트 |
| 실제 E5 smoke | 준비 → 원본 평가 → 2-step 학습 → 1-step checkpoint 재개 → 선정 모델 평가 통과 |
| 가중치 변경 | 첫 step과 두 번째 step의 safetensors hash가 달라짐 |
| 재개 정확성 | CPU에서 재개한 두 번째 step의 safetensors hash가 중단 없는 두 번째 step과 동일 |
| 일괄 실행 | `run.py --skip-prepare`로 baseline → train → tuned 및 pipeline 완료 상태 확인 |
| 공식 LCA 입력 | 고정 revision의 py/test Parquet 50개 row를 읽고 그중 질문 1개를 준비·평가 |
| 실제 snapshot | `archivebox/archivebox`의 수정 전 commit에서 356개 청크 생성, 정답 파일 포함 확인 |
| 원본/학습 모델 평가 | 같은 prepared ID로 질문과 청크를 각각 재인코딩, LCA file 및 valid function 평가 정상 저장 |
| 노트북 | 결과가 없는 경우와 smoke 결과가 있는 경우 모두 셀 실행 통과, 그래프 이미지 확인 |
| 실행 도움말 | prepare/train/evaluate/run/smoke의 `--help` 정상 |
| 코드 기본 검사 | compileall 및 변경 문서 diff 검사 통과 |

## 이 PC에 남긴 결과

```text
data/experiments/e001-smoke/20261011T063051Z/
├─ config.json
├─ smoke-result.json            # 가중치 변경·재개 검증 결과
├─ analysis-check.png           # 실제 학습 곡선
├─ prepared/                    # 수작업 train 6쌍 / valid 3쌍 / 질문 2개 / 청크 3개
├─ official-prepared-v2/        # 공식 LCA 질문 1개 / 청크 356개
└─ runs/
   ├─ baseline / train / resumed / tuned
   ├─ pipeline_smoke_pipeline   # 일괄 실행 상태 completed
   ├─ pipeline_smoke_baseline
   ├─ pipeline_smoke_train      # 실제 체크포인트 보존
   ├─ pipeline_smoke_tuned
   ├─ official_baseline / official_tuned
   └─ valid_smoke
```

초기 `official-prepared/`의 실패 기록도 남아 있습니다. Parquet row에 datetime이 포함되어 있던 처리 문제를 수정했고 `official-prepared-v2/`에서 성공했습니다. 검증 코드는 같은 문제의 회귀 테스트를 포함합니다.

Smoke 자체가 만든 train/resumed의 큰 가중치·optimizer 파일은 기본 정책에 따라 검증 후 삭제했습니다. 설정·history·평가 결과는 남겼으며, 추가 일괄 실행의 `pipeline_smoke_train` 체크포인트는 보존했습니다. 위 데이터는 Git에서 제외되므로 다른 PC에서는 `smoke.py`로 다시 생성합니다.

노트북에서 현재 결과를 보는 설정:

```python
# 첫 코드 셀
CONFIG_PATH = root / 'data/experiments/e001-smoke/20261011T063051Z/config.json'

# 실행 선택 셀
RUN_NAME = 'pipeline_smoke_train'
EVAL_RUN_NAME = 'pipeline_smoke_tuned'
# 실제 저장소 파일 순위는 EVAL_RUN_NAME = 'official_tuned'
```

수작업 학습의 train loss는 step 1 `0.04047`, step 2 `0.02128`이었습니다. 질문 2개·후보 파일 3개의 검색 지표와 공식 LCA 질문 한 개의 지표는 원본/학습 모델 모두 1.0이었습니다. 작은 후보군에서의 성공으로 검색 성능이 개선됐다고 주장할 수 없습니다.

## 아직 측정하지 않은 내용

정식 CodeSearchNet 20,000쌍 학습, LCA 50개 전체 평가, 최대 길이 batch 8의 GPU 메모리·소요 시간, 한국어 서비스 정답 질문 평가는 아직 실행하지 않았습니다. 최초 구현에서는 같은 ZIP/GZ/split 구조의 테스트로 reader를 확인했으며, 이후 아래 다운로드 오류 수정에서 실제 배포 파일을 검증했습니다.

서비스 모델 교체와 기존 문서 재벡터화는 수행하지 않았습니다. 정식 실험 결과를 확인한 뒤 별도의 모델 profile로 적용합니다.

## CodeSearchNet 다운로드 403 수정

사용자의 첫 정식 실행은 기존 S3 `python.zip` 다운로드에서 HTTP 403으로 실패했습니다. 학습은 시작되지 않았고, `data/experiments/e001/runs/e001_001_pipeline/`에 실패 기록이 남아 있습니다.

기본 다운로드를 [Hugging Face CodeSearchNet](https://huggingface.co/datasets/code-search-net/code_search_net)의 고정 revision `bd0cf261e357a3eb5c8fba490d23ec1a1cd59555` Python Parquet으로 변경했습니다. 필요한 컬럼만 배치로 읽고 기존 학습 쌍 스키마로 변환합니다. 로컬 원본 ZIP/JSONL도 계속 읽습니다.

실제 네트워크 다운로드 및 Parquet 읽기 결과:

| 파일 | 크기 | row 수 |
| --- | --- | --- |
| python-train.parquet | 521,785,752 bytes | 412,178 |
| python-valid.parquet | 30,742,555 bytes | 23,107 |

두 파일은 `data/experiments/e001/raw/codesearchnet/<revision>/`에 저장했습니다. 원본 train/valid의 저장소 중복은 0개였고, train에는 12,231개 저장소가 있습니다. 컬럼 변환·split 정규화 테스트를 추가하여 전체 11개 테스트가 통과했습니다. 이 수정 검증에서는 모델 학습을 실행하지 않았습니다.

재실행은 기존 실패 기록과 겹치지 않는 이름으로 진행합니다. 완료된 원본 파일은 재사용합니다.

```powershell
& $e001Python experiments/e001_e5/run.py --download --prefix e001_002
```
