"""제외 항목도 목록에 남기고, 검증된 UTF-8 원문만 수집한다."""

import hashlib
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode

from .config import validate_path
from .github_client import APIError, check_sha
from .storage import stable_id


def excluded_reason(entry, scope, settings):
    path = validate_path(entry['path'])
    mode = entry.get('mode')
    if mode == '120000':
        return 'symlink'
    if mode == '160000' or entry.get('type') == 'commit':
        return 'submodule'
    if entry.get('type') != 'blob' or mode not in ('100644', '100755'):
        raise APIError('invalid_entry', '지원하지 않는 Git 종류 또는 mode입니다.')
    if scope and not path.startswith(scope + '/'):
        return 'outside_scope'
    parts = [part.lower() for part in path.split('/')]
    if any(part in settings['excluded_directories'] for part in parts[:-1]):
        return 'excluded_directory'
    name, suffix = parts[-1], PurePosixPath(path).suffix.lower()
    if (name in settings['credential_names'] or name.startswith('.env.')
            or suffix in settings['credential_extensions']):
        return 'credential_file'
    if suffix not in settings['extensions'] and name not in settings['extensionless_names']:
        return 'unsupported_extension'
    size = entry.get('size')
    if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise APIError('metadata_mismatch', '파일 size metadata가 올바르지 않습니다.')
    if size is not None and size > settings['max_file_bytes']:
        return 'too_large'
    check_sha(entry.get('sha'))
    return None


def collect(client, entries, repo, commit, run_id, scope, settings):
    files, documents = [], []
    aborted = False
    for entry in sorted(entries, key=lambda item: item['path']):
        if entry.get('type') == 'tree':
            continue
        path = entry['path']
        kind = 'symlink' if entry.get('mode') == '120000' else (
            'submodule' if entry.get('mode') == '160000' or entry.get('type') == 'commit' else 'file')
        source_url = f'https://github.com/{repo}/{"tree" if kind == "submodule" else "blob"}/{commit}/{quote(path, safe="/")}'
        size = entry.get('size')
        row = dict(path=path, entry_type=kind, mode=entry.get('mode', ''),
                   git_object_sha=entry.get('sha', ''),
                   size_bytes=size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                   status='excluded', reason_code=None, reason=None, document_id=None,
                   chunk_count=0, embedded_chunk_count=0, source_url=source_url, view_hash=None)
        files.append(row)
        try:
            reason = excluded_reason(entry, scope, settings)
            if reason:
                row.update(reason_code=reason, reason=reason)
                continue
            if aborted:
                raise APIError('run_aborted', '인증·호출 제한으로 본문 수집이 중단되었습니다.')
            data = client.blob(entry['sha'], entry.get('size'), settings['max_file_bytes'])
            row['size_bytes'] = len(data)
            if len(data) > settings['max_file_bytes']:
                row.update(reason_code='too_large', reason='본문 크기 제한 초과')
                continue
            try:
                text = data.decode('utf-8', errors='strict')
            except UnicodeDecodeError:
                row.update(reason_code='unsupported_encoding', reason='UTF-8로 해석할 수 없습니다.')
                continue
            if '\x00' in text:
                row.update(reason_code='binary_content', reason='본문에 NUL 문자가 있습니다.')
                continue
            if text.startswith('version https://git-lfs.github.com/spec/v1'):
                row.update(reason_code='lfs_pointer', reason='Git LFS 실제 본문은 수집하지 않습니다.')
                continue
            document_id = stable_id([repo, commit, path])
            documents.append(dict(document_id=document_id, repo=repo, commit_sha=commit, path=path,
                                  blob_sha=entry['sha'], content_sha256=hashlib.sha256(data).hexdigest(),
                                  size_bytes=len(data), encoding='utf-8', text=text,
                                  line_count=len(text.splitlines(keepends=True)), source_url=source_url))
            row.update(status='collected', document_id=document_id,
                       view_hash='#' + urlencode(dict(run=run_id, document=document_id)))
        except ValueError as error:
            row.update(status='failed', reason_code='invalid_path', reason=str(error))
        except APIError as error:
            row.update(status='excluded' if error.code == 'too_large' else 'failed',
                       reason_code=error.code, reason=str(error))
            aborted = aborted or error.fatal
    return files, documents
