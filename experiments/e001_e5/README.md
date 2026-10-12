# E001 · E5 코드 검색 fine-tuning

[실험 목록](../README.md) · [계획서](../../docs/experiments/E001%20-%20E5%20코드%20검색%20모델%20Fine-tuning%20계획서.md) · [설정](config.json) · [분석 노트북](analysis.ipynb) · [검증 기록](validation.md)

CodeSearchNet 준비 → 원본 E5 평가 → InfoNCE 학습 → 선정 모델 평가 → 결과 분석까지 구현했습니다. 모델은 `AutoTokenizer`와 `AutoModel`로 불러옵니다. 실험은 파일만 사용하며 서비스 DB와 활성 모델을 변경하지 않습니다.

## 목차

- [파일 역할](#파일-역할)
- [빠른 실행](#빠른-실행)
- [단계별 명령어](#단계별-명령어)
- [입력과 전처리 조건](#입력과-전처리-조건)
- [학습과 평가 규칙](#학습과-평가-규칙)
- [저장 구조와 스키마](#저장-구조와-스키마)
- [분석과 자원](#분석과-자원)

## 파일 역할

| 파일 | 역할 |
| --- | --- |
| [config.json](config.json) | 모델·데이터·학습·평가·출력 경로 설정 |
| [prepare.py](prepare.py) | 원본 읽기, 필터·샘플링, 수정 전 저장소 청킹, prepared 저장 |
| [train.py](train.py) | 실제 E5 가중치 학습, validation, 체크포인트 선정·재개 |
| [evaluate.py](evaluate.py) | 질문과 후보를 같은 모델로 재인코딩, 파일 순위와 지표 저장 |
| [run.py](run.py) | 준비·baseline·학습·최종 평가를 순서대로 실행 |
| [retrieval.py](retrieval.py) | prefix, masked mean pooling, 정규화, cosine, 지표 계산 |
| [common.py](common.py) | 설정 검증, 파일 hash, 모델 캐시, 실행 기록 |
| [smoke.py](smoke.py) | 작은 수작업 데이터로 실제 학습·재개까지 검증 |
| [analysis.ipynb](analysis.ipynb) | 저장된 설정·지표·학습 곡선·질문별 순위 조회 |
| [tests/test_experiment.py](tests/test_experiment.py) | 전처리·지표·무결성·파일 경로 검증 |

## 빠른 실행

프로젝트 루트의 PowerShell에서 실행합니다. 지정한 venv에 필요한 라이브러리는 이미 설치되어 있습니다. 버전은 [requirements.txt](requirements.txt)에 기록했습니다. 다른 환경에서는 해당 파일로 설치하되 PyTorch CUDA 빌드는 GPU 환경에 맞게 선택하세요.

```powershell
$e001Python = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'

# 작은 데이터로 실제 E5 준비·평가·2-step 학습·재개 검증
& $e001Python experiments/e001_e5/smoke.py

# 첫 정식 실험: 공식 데이터 다운로드부터 최종 평가까지
& $e001Python experiments/e001_e5/run.py --download --prefix e001_001

# 이미 준비된 데이터를 그대로 재사용하는 다음 실험
& $e001Python experiments/e001_e5/run.py --skip-prepare --prefix e001_002
```

원본 E5가 로컬 캐시에 없다면 먼저 아래 명령으로 받습니다. 실험 코드 자체의 모델 로딩은 로컬 파일만 사용합니다.

```powershell
& $e001Python backend/manage.py download-models --encoder-only
```

Smoke는 `data/experiments/e001-smoke/<UTC시간>/`에 결과를 남깁니다. 기본 실행은 검증을 마친 뒤 **그 smoke가 만든 가중치·optimizer 파일만** 삭제합니다. 다시 모델을 불러올 목적이면 `smoke.py --keep-models`를 사용하세요. 수작업 질문 2개의 지표는 품질 개선의 근거가 아닙니다.

## 단계별 명령어

```powershell
# 1. 원본 다운로드 및 고정 데이터 준비
& $e001Python experiments/e001_e5/prepare.py --download

# 2. 원본 E5의 LCA 파일 검색 평가
& $e001Python experiments/e001_e5/evaluate.py --run-name baseline_001

# 3. 학습, valid MRR@10으로 best 선정
& $e001Python experiments/e001_e5/train.py --run-name ft_001

# 4. 선정 모델의 같은 LCA 평가
& $e001Python experiments/e001_e5/evaluate.py --run-name tuned_001 --model data/experiments/e001/runs/ft_001

# 선택: valid 함수 검색 지표를 별도 실행에 저장
& $e001Python experiments/e001_e5/evaluate.py --run-name valid_001 --dataset valid

# 짧은 학습 확인: 전체 학습 결과와 구분해서 기록
& $e001Python experiments/e001_e5/train.py --run-name short_001 --max-steps 10 --device cpu

# 재개: 같은 데이터·학습 설정·max_steps·장치 설정으로 새 run 생성
& $e001Python experiments/e001_e5/train.py --run-name ft_001_resume --resume data/experiments/e001/runs/ft_001/checkpoints/step_000250

# 인자 설명
& $e001Python experiments/e001_e5/prepare.py --help
& $e001Python experiments/e001_e5/train.py --help
& $e001Python experiments/e001_e5/evaluate.py --help
& $e001Python experiments/e001_e5/run.py --help
```

| 실행 파일 | 주요 인자 |
| --- | --- |
| 공통 | `--config FILE`: 설정 파일. 기본은 이 폴더의 config.json |
| prepare | `--download`, `--train-input PATH`, `--valid-input PATH`, `--lca-input FILE`, `--snapshots DIR`, `--output DIR`, `--limit-eval-queries N` |
| train | `--run-name NAME` 필수, `--prepared DIR`, `--device auto/cpu/cuda`, `--max-steps N`, `--resume CHECKPOINT` |
| evaluate | `--run-name NAME` 필수, `--model CHECKPOINT_OR_RUN`, `--prepared DIR`, `--dataset lca/valid`, `--device auto/cpu/cuda` |
| run | `--prefix NAME`, `--download`, `--skip-prepare`, `--device auto/cpu/cuda`, `--max-steps N` |

비어 있는 prepared 폴더는 사용할 수 있지만, 데이터가 들어 있는 폴더와 기존 run은 덮어쓰지 않습니다. 전처리 조건을 바꾸면 새 config의 `paths.prepared`를 지정하고 준비를 다시 실행하세요. 학습 설정만 바꿀 때는 같은 prepared를 재사용합니다. 출력은 `data/experiments/` 안으로 제한합니다.

## 입력과 전처리 조건

- **CodeSearchNet**: 기본 다운로드는 [Hugging Face 배포본](https://huggingface.co/datasets/code-search-net/code_search_net)의 Python train/validation Parquet입니다. `config.data.hub_revision`으로 버전을 고정합니다. 기존 S3 ZIP 주소는 403이 발생해 사용하지 않습니다. 로컬 v2 ZIP, JSONL, JSONL.GZ도 입력할 수 있습니다.
- **컬럼 변환**: Parquet의 `repository_name → repo`, `func_path_in_repository → path`, `func_documentation_string → docstring`, `whole_func_string → code`, `split_name → partition`을 변환합니다. `validation`은 `valid`로 맞춥니다. Parquet은 배치마다 읽어 전체 원본을 한 번에 메모리에 올리지 않습니다.
- **LCA**: 고정 revision의 `py/test` Parquet 또는 같은 필드의 JSONL. `text_id`, `repo_owner`, `repo_name`, `base_sha`, `changed_files`, `issue_title`, `issue_body`를 읽습니다. `changed_files`는 목록 또는 목록 형태 문자열입니다.
- **스냅샷**: `raw/snapshots/owner__repo/<base_sha>/파일.py` 또는 `owner__repo/<base_sha>.zip`. 없으면 `--download`에서 GitHub의 해당 commit ZIP을 받습니다. ZIP은 디스크에 풀지 않습니다.

원본 train/valid를 유지하고 LCA test의 **모든 저장소**를 학습·valid에서 제외합니다. train에는 원본 valid에 등장한 저장소도 넣지 않습니다. 코드 AST에서 질문과 일치하는 함수 docstring을 제거하고, 정규화한 질문과 AST가 중복되면 제외합니다. 이 비교는 train/valid 사이에도 적용합니다. AST를 다시 코드로 출력하므로 학습 코드의 포맷·주석은 원본과 달라질 수 있습니다.

질문·코드가 prefix와 special token을 포함해 512토큰을 넘는 학습 쌍은 제외합니다. 필터 후 seed 기반 reservoir sampling으로 train 20,000쌍, valid 1,000쌍을 고릅니다. 부족하면 자동으로 수를 줄이지 않고 오류를 냅니다.

평가 후보는 수정 전 `base_sha`의 UTF-8 `.py` 파일입니다. test 파일도 포함하며, 1MB 초과·바이너리·심볼릭 링크는 제외합니다. 서비스의 청커를 그대로 사용합니다. 정답 파일이 하나라도 후보에서 빠지면 **질문 전체를 제외**하고 `excluded_queries.jsonl`에 이유를 남깁니다. 질문 제목+본문은 최대 500토큰을 앞/뒤에서 남기고, prefix+special 포함 512 이하를 다시 확인합니다.

`--limit-eval-queries`는 ID 정렬 후 앞 N개만 준비합니다. 부분 평가 여부와 제외 수가 결과에 기록됩니다. 정식 결과를 비교할 때 평가 범위와 제외 비율도 함께 보세요.

## 학습과 평가 규칙

원본 모델은 [intfloat/multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small), revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`입니다. 출력은 384차원, masked mean pooling 후 L2 정규화합니다.

```text
학습: query: docstring ↔ passage: 코드
      B×B cosine / temperature → 정답은 대각선 → cross entropy(InfoNCE)
      같은 배치의 다른 코드 B-1개가 negative

검증: CodeSearchNet valid 전체 함수 후보에서 MRR@10
      가장 높은 체크포인트 선정, 동률이면 먼저 선정한 모델 유지

최종: LCA test 질문과 모든 후보 청크를 선정 모델로 각각 다시 인코딩
      모든 청크 cosine → 파일마다 최대 점수 → 파일 Top K
      질문별 Recall@5 / MRR@10 / binary nDCG@10 → 질문 평균
```

E5 전체 가중치를 AdamW로 학습합니다. gradient checkpointing을 사용하며 CUDA에서 BF16 지원 시 BF16 autocast, 그 외 FP32입니다. 배치가 1개 남으면 그 쌍은 해당 epoch에서 제외합니다. 실제 대조학습 배치가 negative 수를 결정하며 현재 gradient accumulation은 사용하지 않습니다.

Train loss는 해당 배치의 negative, valid loss는 valid 전체 후보의 negative를 사용하므로 **두 loss의 절댓값을 직접 비교하지 않습니다.** 검색 개선은 baseline과 tuned의 같은 데이터·같은 후보 지표로 판단합니다. 최종 test는 체크포인트 선정에 사용하지 않습니다.

체크포인트에는 모델·tokenizer·optimizer·scheduler·step·다음 배치·PyTorch RNG를 저장합니다. 재개는 마지막 저장 시점부터 이어지고 새 run으로 기록합니다. 본인이 생성한 로컬 체크포인트를 사용하세요. 완료된 목표 step에서 더 학습하는 기능은 재개가 아니라 새 실험으로 진행합니다.

## 저장 구조와 스키마

```text
data/experiments/e001/
├─ raw/                         # 공식 ZIP/Parquet와 commit별 snapshot
├─ prepared/
│  ├─ config.json               # 전처리 당시 설정
│  ├─ manifest.json             # 완료 상태·원본/결과 hash·필터 수·데이터 ID
│  ├─ train.jsonl / valid.jsonl  # 한 줄 = 설명/코드 쌍
│  ├─ eval_queries.jsonl        # 한 줄 = 평가 질문과 정답 파일들
│  ├─ eval_chunks.jsonl         # 한 줄 = snapshot의 한 청크
│  └─ excluded_queries.jsonl    # 한 줄 = 제외 질문과 이유
└─ runs/
   ├─ baseline_001/             # config, manifest, metrics, rankings
   ├─ ft_001/                   # config, manifest, history, best/latest, checkpoints
   ├─ tuned_001/                # config, manifest, metrics, rankings
   └─ e001_001_pipeline/        # 전체 상태와 자식 run 목록
```

| 파일 | 주요 필드 |
| --- | --- |
| prepared/manifest | `status`, `protocol_hash`, `source_hashes`, `files`, `dataset_id`, `counts`, `filter_stats`, `snapshot_content_hashes`, `evaluation_subset` |
| train/valid row | `pair_id`, `repo`, `path`, `query`, `positive`, `code_hash`, `query_hash` |
| eval_queries row | `query_id`, `question`, `repo`, `commit_sha`, `snapshot_id`, `relevant_ids`, `original_query_tokens`, `query_truncated` |
| eval_chunks row | `repo`, `commit_sha`, `path`, `snapshot_id`, `document_id`, `chunk_id`, `chunk_index`, `text`, `start_line`, `end_line`, `start_offset`, `end_offset`, `token_count`; 준비 시 `embedding_status=not_run` |
| excluded_queries row | `query_id`, `repo`, `base_sha`, `error` |
| runs/manifest | `run_id`, `kind`, `status`, 시간, `dataset_id`, `config_hash`, `git_commit`, `code_hashes`, `versions`, 실제 `device`·`compute_dtype`, 학습/평가 메타데이터 |
| history.csv | `step`, `epoch`, `train_loss`, `val_loss`, `learning_rate`, `validation_mrr_at_10`, `validation_recall_at_5`, `validation_ndcg_at_10`; 검증 없는 step의 검증 칸은 빈칸 |
| metrics.json | `dataset_id`, `model`, `split`, `evaluation_unit`, `query_count`, `metrics`; LCA는 부분 평가·요청/제외 수 포함 |
| rankings row | 질문·정답, `query_vectors_model`, `corpus_vectors_model`, `retrieved`: 파일 ID·경로·순위·점수·정답 여부·대표 청크·라인·원문 |
| best.json / latest.json | `checkpoint` 경로; best에는 선정 지표와 값 포함 |
| checkpoint/ | `model.safetensors`, tokenizer/config, `e001_model.json`(모델/데이터/학습 설정 hash), `training_state.pt`(재개 상태) |

학습 실행과 최종 평가 실행은 폴더가 다릅니다. `ft_001`에서 history를 보고 `tuned_001`에서 질문별 순위를 봅니다. 모든 prepared 파일을 hash로 검증하고, 평가할 학습 모델도 hash를 확인합니다. 벡터는 평가마다 생성해 메모리에서 비교하며 별도 벡터 DB에는 저장하지 않습니다.

## 분석과 자원

VS Code에서 `analysis.ipynb`를 열고 지정한 venv를 kernel로 선택하세요. 첫 셀의 `CONFIG_PATH`로 볼 데이터 영역을 정하고, `RUN_NAME`·`EVAL_RUN_NAME`·`QUERY_ID`로 학습 실행·평가 실행·질문을 선택합니다. 지표 표, 학습 곡선, 정답 파일과 대표 청크를 확인할 수 있습니다.

`auto`는 `torch.cuda.is_available()` 기준입니다. CUDA 메모리가 부족하면 자동 CPU 재시도하지 않습니다. 현재 Qwen 서비스가 GPU를 점유하고 있다면 정식 GPU 학습 전에 서비스를 직접 종료하거나 `--device cpu`를 사용하세요. Smoke는 CPU를 사용합니다. 정식 길이·batch 8의 GPU 메모리와 전체 소요 시간은 아직 측정하지 않았습니다.

스냅샷 크기와 체크포인트 개수에 따라 디스크 사용량이 늘어납니다. 체크포인트마다 FP32 모델과 AdamW 상태로 대략 1.5GB 이상의 공간이 필요할 수 있습니다. 현 구현은 저장한 validation 체크포인트를 모두 보존합니다.

다운로드 단계에서 실패하면 학습은 시작되지 않습니다. 재시도할 때 기존 실패 기록을 보존하도록 새 prefix를 사용하세요. 예: `run.py --download --prefix e001_002`. CodeSearchNet train 약 522MB, validation 약 31MB를 받으며 완료된 파일은 재사용합니다.

이번 학습은 설명↔함수 검색이고 LCA는 영어 이슈↔파일 검색입니다. 한국어 질문이나 서비스 전체 레포 검색 품질은 별도의 정답 질문으로 확인해야 합니다. 서비스 적용 시에는 새 모델 profile과 전체 corpus 재벡터화가 별도 작업입니다.
