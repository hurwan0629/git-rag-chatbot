"""GitHub API로 특정 commit의 텍스트 파일 하나를 읽는 수집 실험."""

import argparse
import base64
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
from dotenv import load_dotenv

load_dotenv()

print(str(os.environ.get("GITHUB_TOKEN"))[:10])

def api_json(url):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "git-rag-fetch-test",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        return json.load(response)


def fetch_file(repo_url, path="README.md", ref=None):
    url = urlsplit(repo_url)
    match = re.fullmatch(r"/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+)/?", url.path)
    if url.scheme != "https" or url.netloc != "github.com" or not match or url.query or url.fragment:
        raise ValueError("저장소 루트 URL을 입력하세요: https://github.com/owner/repo")
    owner, repo = match.groups()
    repo = repo.removesuffix(".git")
    if repo in ("", ".", ".."):
        raise ValueError("올바른 저장소 이름을 입력하세요.")
    if not path or "\\" in path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ValueError("저장소 기준 파일 경로를 입력하세요: docs/example.md")

    base_url = f"https://api.github.com/repos/{owner}/{repo}"
    params = {"per_page": 1}
    if ref:
        params["sha"] = ref
    commits = api_json(f"{base_url}/commits?{urlencode(params)}")
    if not commits:
        raise ValueError("읽을 commit이 없습니다.")
    commit = commits[0]["sha"]
    file = api_json(f"{base_url}/contents/{quote(path, safe='/')}?{urlencode({'ref': commit})}")
    # shortcut: 작은 일반 텍스트 파일만 조회한다. 폴더 수집·대용량 파일은 다음 실험에서 확장한다.
    if not isinstance(file, dict) or file.get("type") != "file":
        raise ValueError("폴더 대신 일반 파일 경로를 지정하세요.")
    if file.get("encoding") != "base64":
        raise ValueError("이 테스트는 Contents API의 Base64 응답을 지원하는 작은 파일만 읽습니다.")
    text = base64.b64decode(file["content"]).decode("utf-8")
    return {
        "repo": f"{owner}/{repo}",
        "commit": commit,
        "path": file["path"],
        "source_url": f"https://github.com/{owner}/{repo}/blob/{commit}/{quote(file['path'], safe='/')}",
        "text": text,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_url", help="https://github.com/owner/repo")
    parser.add_argument("--path", default="README.md", help="읽을 파일 경로 (기본: README.md)")
    parser.add_argument("--ref", help="브랜치·태그·commit SHA (기본: 기본 브랜치의 최신 commit)")
    args = parser.parse_args()
    try:
        result = fetch_file(args.repo_url, args.path, args.ref)
    except HTTPError as error:
        print(f"GitHub API 오류 HTTP {error.code}: 경로, 접근 권한, 호출 제한을 확인하세요.", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, ValueError) as error:
        print(f"읽기 실패: {error}", file=sys.stderr)
        return 1
    for key in ("repo", "commit", "path", "source_url"):
        print(f"{key}: {result[key]}")
    print(f"\n--- 원문 미리보기 ({len(result['text'])}자 중 최대 1,000자) ---")
    print(result["text"][:1000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
