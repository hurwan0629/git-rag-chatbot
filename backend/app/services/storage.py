"""ID 생성, 원자적 파일 저장, 원문·청크·벡터의 재로드 검증."""

import csv
import hashlib
import json
import re
from bisect import bisect_right
from pathlib import Path

FILE_COLUMNS = ['path', 'entry_type', 'mode', 'git_object_sha', 'size_bytes', 'status',
                'reason_code', 'reason', 'document_id', 'chunk_count', 'embedded_chunk_count',
                'source_url', 'view_hash']


def stable_id(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def read_files(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for key in ('size_bytes', 'chunk_count', 'embedded_chunk_count'):
            row[key] = int(row[key]) if row[key] else None
        for key in ('reason_code', 'reason', 'document_id', 'view_hash'):
            row[key] = row[key] or None
    return rows


def line_starts(text):
    starts, offset = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(offset)
        offset += len(line)
    return starts


def line_range(starts, start, end):
    return bisect_right(starts, start), bisect_right(starts, end - 1)


def validate_dataset(folder, manifest, tokenizer=None):
    import numpy as np
    from .config import CHUNKING, DIMENSION, MAX_INPUT_TOKENS, MODEL_ID, PREFIX
    files = read_files(folder / 'files.csv')
    documents = read_jsonl(folder / 'documents.jsonl')
    chunks = read_jsonl(folder / 'chunks.jsonl')
    vector_path = folder / 'vectors.npy'
    vectors = np.load(vector_path, allow_pickle=False) if vector_path.is_file() else None
    for document in documents:
        if (folder / 'raw' / (document['document_id'] + '.txt')).read_bytes() != document['text'].encode('utf-8'):
            raise ValueError('raw content mismatch')
    return validate_records(files, documents, chunks, vectors, manifest, tokenizer)


def validate_records(files, documents, chunks, vectors, manifest, tokenizer=None):
    import numpy as np
    from .config import CHUNKING, DIMENSION, MAX_INPUT_TOKENS, MODEL_ID, PREFIX
    counts = manifest['counts']

    def require(condition, message):
        if not condition:
            raise ValueError('저장 검증 실패: ' + message)

    require(counts['listed_files'] == len(files), '목록 건수')
    for status, key in [('collected', 'collected_files'), ('excluded', 'excluded_files'), ('failed', 'failed_files')]:
        require(counts[key] == sum(row['status'] == status for row in files), key)
    require(sum(counts[key] for key in ('collected_files', 'excluded_files', 'failed_files')) == len(files), '파일 상태')
    require(counts['documents'] == len(documents) == counts['collected_files'], '문서 건수')
    require(counts['chunks'] == len(chunks), '청크 건수')
    docs = {item['document_id']: item for item in documents}
    require(len(docs) == len(documents), 'document ID 중복')
    require(len({row['path'] for row in files}) == len(files), '파일 경로 중복')
    require(len({item['chunk_id'] for item in chunks}) == len(chunks), 'chunk ID 중복')
    by_document = {identifier: [] for identifier in docs}
    starts_by_document = {}
    for document in documents:
        text = document['text']
        starts_by_document[document['document_id']] = line_starts(text)
        require(document['repo'] == manifest['repo'] and document['commit_sha'] == manifest['commit_sha'], '원문 출처')
        require(document['document_id'] == stable_id([document['repo'], document['commit_sha'], document['path']]), 'document ID')
        data = text.encode('utf-8')
        require(len(data) == document['size_bytes'], '원문 크기')
        require(hashlib.sha256(data).hexdigest() == document['content_sha256'], '원문 hash')
        require(document['line_count'] == len(text.splitlines(keepends=True)), '원문 줄 수')
    rows = []
    for chunk in chunks:
        identifier = chunk['document_id']
        require(identifier in docs, '없는 document 참조')
        document = docs[identifier]
        text = document['text']
        start, end = chunk['start_offset'], chunk['end_offset']
        require(0 <= start < end <= len(text), '문자 구간')
        require(chunk['text'] == text[start:end], '청크 원문 slice')
        require((chunk['start_line'], chunk['end_line']) == line_range(starts_by_document[identifier], start, end), '청크 줄 범위')
        require(chunk['chunk_id'] == stable_id(dict(document_id=identifier, start_offset=start, end_offset=end,
                                                 model_id=MODEL_ID, revision=manifest['embedding']['revision'],
                                                 chunking=CHUNKING)), 'chunk ID')
        require(0 < chunk['token_count'] <= MAX_INPUT_TOKENS, '입력 token 길이')
        if tokenizer is not None:
            require(len(tokenizer(PREFIX + chunk['text'], add_special_tokens=True, truncation=False,
                                  verbose=False)['input_ids']) == chunk['token_count'], 'token 수 불일치')
        require(chunk['embedding_status'] in ('not_run', 'embedded', 'failed'), 'embedding 상태')
        if chunk['embedding_status'] == 'embedded':
            require(isinstance(chunk['vector_row'], int) and chunk['vector_row'] >= 0, 'vector 행 번호')
            rows.append(chunk['vector_row'])
        else:
            require(chunk['vector_row'] is None, '미생성 vector 참조')
        by_document[identifier].append(chunk)
    if vectors is None:
        require(not rows and counts['vectors'] == 0, '누락된 NPY')
        require(manifest['mode'] == 'collect_only' or manifest['status'] in ('partial', 'failed', 'running'), 'embed NPY 누락')
    else:
        require(manifest['mode'] == 'embed', 'collect-only NPY')
        require(vectors.ndim == 2 and vectors.shape == (len(rows), DIMENSION), 'vector shape')
        require(vectors.dtype == np.float32 and counts['vectors'] == len(rows), 'vector dtype/건수')
        require(sorted(rows) == list(range(len(rows))), 'vector 행 중복·누락')
        require(np.isfinite(vectors).all(), 'vector 유한값')
        if len(vectors):
            require(np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4), 'vector 정규화')
    for row in files:
        if row['status'] != 'collected':
            require(row['document_id'] is None and row['chunk_count'] == 0 and row['embedded_chunk_count'] == 0, '제외·실패 연결')
            continue
        require(row['document_id'] in docs, '파일 document 참조')
        document = docs[row['document_id']]
        require(row['path'] == document['path'] and row['git_object_sha'] == document['blob_sha'], '파일 출처 대응')
        grouped = by_document[row['document_id']]
        require(row['chunk_count'] == len(grouped), '파일 청크 수')
        require(row['embedded_chunk_count'] == sum(item['embedding_status'] == 'embedded' for item in grouped), '파일 vector 수')
        # 청킹 전 단계가 실패한 partial run은 원문을 보존하며, 청킹 완료 문서의 연결은 엄격하게 확인한다.
        if document['text'] and not grouped and manifest['status'] in ('running', 'partial', 'failed'):
            continue
        offset = 0
        for index, chunk in enumerate(sorted(grouped, key=lambda item: item['chunk_index'])):
            require(chunk['chunk_index'] == index and chunk['start_offset'] == offset, '청크 누락·겹침')
            offset = chunk['end_offset']
        require(offset == len(document['text']), '청크 전체 원문 범위')
    return files, documents, chunks
