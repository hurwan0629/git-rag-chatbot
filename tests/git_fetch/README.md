# GitHub 파일 수집 테스트

GitHub REST API에 HTTPS 요청을 보내 텍스트 파일 하나를 읽는 실험입니다.
`git fetch` 명령어를 실행하는 코드는 아닙니다. 사용자 지정 venv를 사용하며 외부 패키지 추가 설치는 필요 없습니다.

## 실행

프로젝트 루트의 PowerShell에서 실행합니다.

```powershell
$gitRagPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $gitRagPython tests/git_fetch/fetch_github.py https://github.com/octocat/Hello-World --path README
```

자신이 확인하려는 공개 저장소 URL과 코드·Markdown 파일 경로로 바꿔 실행하세요.

```powershell
& $gitRagPython tests/git_fetch/fetch_github.py https://github.com/OWNER/REPO --path docs/example.md --ref main
```

- URL은 `https://github.com/owner/repo` 또는 `.git`으로 끝나는 저장소 루트 링크입니다.
- `--path`는 저장소 기준 파일 경로이며 기본값은 `README.md`입니다.
- `--ref`는 브랜치·태그·commit SHA입니다. 생략하면 기본 브랜치의 최신 commit을 사용합니다.
- 먼저 commit SHA를 확정하고 그 SHA로 파일을 읽습니다. 출력되는 출처 링크도 같은 commit을 가리킵니다.
- 저장소, commit, 경로, 출처 링크와 원문 앞 1,000자를 출력합니다. `fetch_file()` 반환값에는 전체 원문이 들어 있습니다.
- 선택적으로 환경 변수 `GITHUB_TOKEN`에 기존 토큰을 설정하면 인증 요청을 보냅니다. 토큰을 코드에 적지 마세요.

현재 범위는 작은 UTF-8 일반 파일 하나의 조회입니다. 폴더 재귀 수집, DB 적재, 청킹, 임베딩은 포함하지 않습니다.

## 네트워크 없이 검증

```powershell
$gitRagPython = 'C:\LANG_CHAIN_2026\2026-05-19_KDT_lang_chain\.venv\Scripts\python.exe'
& $gitRagPython -m unittest discover -s tests/git_fetch -p "test_*.py" -v
```

잘못된 입력의 요청 차단, commit 고정, 한글 원문 복원, 폴더 입력 거부를 확인합니다.

API 참고: [Contents API](https://docs.github.com/en/rest/repos/contents), [List commits](https://docs.github.com/en/rest/commits/commits#list-commits).
