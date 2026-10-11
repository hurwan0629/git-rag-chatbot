"""기존 output을 검증하고 repo별 최신 완료 run을 메모리에 고정한다."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import numpy as np

from repo_ingestion.config import CHUNKING, DIMENSION, MODEL_ID, OUTPUT_DIR, validate_path
from repo_ingestion.storage import stable_id, validate_dataset
from . import APIError

E5_REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
EMBEDDING = dict(model_id=MODEL_ID, revision=E5_REVISION, prefix='passage: ',
                 pooling='masked_mean', normalize=True, dimension=DIMENSION, dtype='float32')


class Catalog:
    def __init__(self, root=OUTPUT_DIR):
        self.root = Path(root).resolve()
        self.repos, self.skipped_runs, self.runs = [], [], {}
        groups = {}
        for path in sorted(self.root.glob('*/*/manifest.json')):
            run_id, repo = path.parent.name, None
            try:
                if not path.resolve().is_relative_to(self.root):
                    raise ValueError('output 밖의 manifest입니다.')
                manifest = json.loads(path.read_text(encoding='utf-8'))
                repo = manifest['repo']
                if not isinstance(repo, str) or not re.fullmatch(r'[a-z0-9-]+/[a-z0-9_.-]+', repo):
                    raise ValueError('repo ID 형식 오류')
                if manifest['run_id'] != run_id or path.parent.parent.name != repo.replace('/', '--'):
                    raise ValueError('폴더와 repo/run ID가 다릅니다.')
                groups.setdefault(repo, []).append((path.parent, manifest))
            except Exception as error:
                self.skipped_runs.append(dict(repo=repo if isinstance(repo, str) else None,
                                              run_id=run_id, reason=f'manifest 오류: {type(error).__name__}'))

        used_ids = set()
        for repo, candidates in sorted(groups.items()):
            completed = [item for item in candidates if item[1].get('status') == 'completed']
            record = dict(repo=repo, run_id=None, commit_sha=None, documents=0, chunks=0,
                          searchable=False, reason='완료된 run이 없습니다.')
            selected = max(completed, key=self._run_order) if completed else None
            for folder, manifest in candidates:
                if selected and folder == selected[0]:
                    continue
                reason = ('이전 완료 run' if manifest.get('status') == 'completed'
                          else '검색 제외 상태: ' + str(manifest.get('status', 'unknown')))
                self.skipped_runs.append(dict(repo=repo, run_id=manifest['run_id'], reason=reason))
            if selected:
                folder, manifest = selected
                record.update(run_id=manifest['run_id'], commit_sha=manifest.get('commit_sha'))
                try:
                    self._validate_manifest(manifest)
                    if manifest['run_id'] in used_ids:
                        raise ValueError('다른 저장소와 run ID가 중복됩니다.')
                    used_ids.add(manifest['run_id'])
                    if not folder.resolve().is_relative_to(self.root):
                        raise ValueError('output 밖의 run입니다.')
                    files, documents, chunks = validate_dataset(folder, manifest)
                    vectors = np.load(folder / 'vectors.npy', allow_pickle=False)
                    if not len(vectors):
                        raise ValueError('검색할 벡터가 없습니다.')
                    if any(chunk['embedding_status'] != 'embedded' for chunk in chunks):
                        raise ValueError('완료 run에 미생성 벡터가 있습니다.')
                    rows = [None] * len(vectors)
                    docs = {doc['document_id']: doc for doc in documents}
                    for doc in documents:
                        validate_path(doc['path'])
                        doc['source_url'] = ('https://github.com/' + repo + '/blob/'
                                             + manifest['commit_sha'] + '/' + quote(doc['path'], safe='/'))
                    for chunk in chunks:
                        rows[chunk['vector_row']] = chunk
                    self.runs[repo] = dict(manifest=manifest, documents=docs, chunks=rows,
                                           vectors=vectors, files=files)
                    record.update(documents=len(documents), chunks=len(chunks), searchable=True, reason=None)
                except Exception as error:
                    record['reason'] = ('데이터 오류: ' + str(error) if isinstance(error, ValueError)
                                        else '데이터 로딩 오류: ' + type(error).__name__)
            self.repos.append(record)
        selected_runs = [dict(repo=r['repo'], run_id=r['run_id'], commit_sha=r['commit_sha'],
                              searchable=r['searchable']) for r in self.repos]
        self.catalog_id = stable_id(dict(runs=selected_runs, embedding=EMBEDDING))

    @staticmethod
    def _run_order(item):
        manifest = item[1]
        try:
            finished = datetime.fromisoformat(manifest['finished_at'])
            if finished.tzinfo is None:
                raise ValueError('시간대 누락')
            finished = finished.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError):
            # 완료 run의 시간 정보가 손상되면 이전 데이터로 조용히 대체하지 않는다.
            finished = datetime.max.replace(tzinfo=timezone.utc)
        return finished, manifest['run_id']

    @staticmethod
    def _validate_manifest(manifest):
        if manifest.get('schema_version') != '1.0' or manifest.get('mode') != 'embed':
            raise ValueError('지원하지 않는 schema/mode입니다.')
        if manifest.get('inventory_complete') is not True:
            raise ValueError('파일 목록 수집이 완료되지 않았습니다.')
        if not isinstance(manifest.get('commit_sha'), str) or not re.fullmatch(r'[0-9a-f]{40}', manifest['commit_sha']):
            raise ValueError('commit SHA가 유효하지 않습니다.')
        finished = datetime.fromisoformat(manifest['finished_at'])
        if finished.tzinfo is None:
            raise ValueError('finished_at 시간대가 없습니다.')
        settings = manifest.get('embedding', {})
        if any(settings.get(k) != v or type(settings.get(k)) is not type(v) for k, v in EMBEDDING.items()):
            raise ValueError('E5 모델 revision 또는 임베딩 설정이 다릅니다.')
        if manifest.get('chunking') != CHUNKING:
            raise ValueError('지원하지 않는 청킹 설정입니다.')

    def document(self, run_id, document_id):
        for run in self.runs.values():
            if run['manifest']['run_id'] == run_id and document_id in run['documents']:
                doc = run['documents'][document_id]
                return dict(run_id=run_id, **{key: doc[key] for key in
                            ('repo', 'commit_sha', 'document_id', 'path', 'text', 'source_url')})
        raise APIError(404, 'DOCUMENT_NOT_FOUND', '해당 run의 문서를 찾을 수 없습니다.')

    def snapshot(self):
        return dict(catalog_id=self.catalog_id, repos=self.repos, skipped_runs=self.skipped_runs)
