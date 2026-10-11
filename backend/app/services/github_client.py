"""GitHub REST API로 immutable commit/tree/blob을 읽는다."""

import base64
import binascii
import json
import hashlib
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class APIError(Exception):
    def __init__(self, code, message, fatal=False):
        super().__init__(message)
        self.code = code
        self.fatal = fatal


def check_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{40}', value):
        raise APIError('metadata_mismatch', 'Git SHA metadata가 올바르지 않습니다.')
    return value


class GitHubClient:
    def __init__(self, repo, token=None):
        self.base = f'https://api.github.com/repos/{repo}'
        self.token = token

    def get(self, path):
        headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'git-rag-ingestion-test'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        try:
            with urlopen(Request(self.base + path, headers=headers), timeout=30) as response:
                result = json.load(response)
        except HTTPError as error:
            response_headers = error.headers or {}
            limited = (error.code == 429 or response_headers.get('X-RateLimit-Remaining') == '0'
                       or response_headers.get('Retry-After'))
            code = 'rate_limit' if limited else ('authentication_error' if error.code == 401 else (
                'permission_or_rate_limit' if error.code == 403 else 'api_error'))
            raise APIError(code, f'GitHub HTTP {error.code}: 권한·repo/ref·호출 제한을 확인하세요.',
                           fatal=error.code in (401, 403, 429)) from None
        except (URLError, TimeoutError, OSError):
            raise APIError('network_error', 'GitHub 요청에 실패했습니다. 네트워크 연결을 확인하세요.') from None
        except (ValueError, UnicodeError):
            raise APIError('invalid_response', 'GitHub JSON 응답을 해석할 수 없습니다.') from None
        if not isinstance(result, dict):
            raise APIError('invalid_response', 'GitHub 객체 응답이 필요합니다.')
        return result

    def snapshot(self, ref=None):
        repo = self.get('')
        ref = ref or repo.get('default_branch')
        if not isinstance(ref, str) or not ref:
            raise APIError('invalid_response', '기본 브랜치가 없습니다. 빈 repo인지 확인하세요.')
        commit = self.get('/commits/' + quote(ref, safe=''))
        try:
            return check_sha(commit['sha']), check_sha(commit['commit']['tree']['sha'])
        except (KeyError, TypeError):
            raise APIError('invalid_response', 'commit/tree metadata가 없습니다.') from None

    def tree(self, sha, recursive=False):
        result = self.get('/git/trees/' + check_sha(sha) + ('?recursive=1' if recursive else ''))
        if result.get('sha') != sha or not isinstance(result.get('tree'), list):
            raise APIError('metadata_mismatch', 'tree 응답 metadata가 일치하지 않습니다.')
        entries = result['tree']
        if any(not isinstance(item, dict) or not isinstance(item.get('path'), str) for item in entries):
            raise APIError('invalid_response', 'tree 항목 경로가 올바르지 않습니다.')
        return entries, bool(result.get('truncated'))

    def inventory(self, tree_sha):
        entries, truncated = self.tree(tree_sha, recursive=True)
        if not truncated:
            return entries, True, []
        # 재귀 응답이 잘렸다면 루트부터 비재귀 tree를 순회하여 전체 목록을 다시 만든다.
        found, pending, errors = {}, [('', tree_sha)], []
        while pending:
            prefix, sha = pending.pop()
            try:
                children, cut = self.tree(sha)
                for child in children:
                    item = dict(child, path=prefix + child['path'])
                    found[item['path']] = item
                    if item.get('type') == 'tree' and not cut:
                        pending.append((item['path'] + '/', item.get('sha')))
                if cut:
                    raise APIError('truncated_tree', '하위 tree 목록도 잘려 전체 목록을 확보하지 못했습니다.')
            except APIError as error:
                errors.append(dict(stage='listing', path=prefix or None, code=error.code, message=str(error)))
                if error.fatal:
                    break
        return list(found.values()), not errors, errors

    def blob(self, sha, expected_size, max_bytes):
        result = self.get('/git/blobs/' + check_sha(sha))
        size = result.get('size')
        if result.get('sha') != sha or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise APIError('metadata_mismatch', 'blob SHA 또는 size가 일치하지 않습니다.')
        if size > max_bytes:
            raise APIError('too_large', '본문 byte 크기가 제한을 초과합니다.')
        if result.get('encoding') != 'base64' or not isinstance(result.get('content'), str):
            raise APIError('invalid_base64', 'Base64 blob 본문이 필요합니다.')
        try:
            data = base64.b64decode(''.join(result['content'].split()), validate=True)
        except (ValueError, binascii.Error):
            raise APIError('invalid_base64', 'Base64 blob 본문을 해석할 수 없습니다.') from None
        if len(data) != size or (expected_size is not None and len(data) != expected_size):
            raise APIError('metadata_mismatch', 'blob 본문 byte 수와 size metadata가 다릅니다.')
        actual_sha = hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\x00' + data).hexdigest()
        if actual_sha != sha:
            raise APIError('metadata_mismatch', 'Git blob hash mismatch')
        return data
