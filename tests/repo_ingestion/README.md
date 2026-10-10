# GitHub repo 수집·임베딩 테스트

프로젝트 루트 `git-rag`의 PowerShell에서 실행합니다. 필요한 라이브러리는 지정 venv에 설치되어 있습니다.

```powershell
$gitRagPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $gitRagPython tests/repo_ingestion/run.py https://github.com/octocat/Hello-World --env-file tests/git_fetch/.env
```

완료 후 `output/index.html`을 브라우저로 엽니다. repo/run → 파일 → 청크 링크를 선택하면 원문·청크 경계·token 수·vector_row를 볼 수 있습니다. 모든 repo/run 결과는 하나의 HTML에서 확인합니다.

모델은 `intfloat/multilingual-e5-small`입니다. 최초 실행 시 tokenizer·encoder를 `.model-cache/`에 다운로드합니다. 매 실행 시 모델 revision SHA를 확인하고 같은 버전으로 로딩합니다. 사용한 SHA는 manifest에 기록합니다.

```powershell
# 도움말
& $gitRagPython tests/repo_ingestion/run.py --help

# encoder를 쓰지 않고 수집·청킹만 실행 (tokenizer는 필요)
& $gitRagPython tests/repo_ingestion/run.py https://github.com/octocat/Hello-World --env-file tests/git_fetch/.env --collect-only

# 저장 결과로 HTML만 갱신
& $gitRagPython tests/repo_ingestion/report.py

# 네트워크 없는 자동 테스트
& $gitRagPython -m unittest discover -s tests/repo_ingestion -p 'test_*.py' -v
```

`GITHUG_TOKEN`과 `GITHUB_TOKEN`을 지원하며 토큰 값을 출력하지 않습니다. 기본 대상은 Python·JS/TS·Java/Kotlin 등 코드와 JSON/YAML 등 설정 파일입니다. README·LICENSE·Dockerfile·Makefile·일부 dotfile도 포함합니다. 전체 기본 목록은 `config.py`의 `DEFAULT_EXTENSIONS`, `SUPPORTED_FILENAMES`에 있습니다. `--extensions`는 기본 확장자 목록을 대체합니다. `--ref`, `--scope`, `--device`, `--batch-size`, `--max-file-bytes`는 help를 참고하세요.

확장자 기준이 넓어져도 UTF-8·파일 크기·바이너리·자격 증명 검사는 적용됩니다. 변경한 수집 기준은 새 실행부터 적용하며 이전 run의 원문·청크·벡터를 수정하지 않습니다.

저장 파일은 `output/owner--repo/run_id/`의 manifest.json, files.csv, documents.jsonl, chunks.jsonl, vectors.npy, raw/*.txt입니다. collect-only는 NPY가 없습니다. 종료 코드 0은 정상, 1은 처리 실패, 2는 인자 오류입니다. partial/failed도 저장되며 HTML에서 이유를 볼 수 있습니다.

상세 문서: [최종 설계와 스키마](../../docs/tests/repo-ingestion/final-design.md), [명령어와 사용 방법](../../docs/tests/repo-ingestion/usage.md).
