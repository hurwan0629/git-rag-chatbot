"""같은 E5 revision으로 질문을 인코딩하고 선택 저장소 청크를 비교한다."""

import time

import numpy as np

from repo_ingestion.config import DIMENSION
from repo_ingestion.embedder import load_encoder, load_tokenizer, masked_mean_pool
from . import APIError
from .catalog import E5_REVISION

QUERY_PREFIX = 'query: '


def rank_sources(catalog, vector, repos, top_k):
    if (vector.shape != (DIMENSION,) or not np.isfinite(vector).all()
            or not np.isclose(np.linalg.norm(vector), 1, atol=1e-4)):
        raise ValueError('질문 벡터 검증 실패')
    ranked = []
    for repo in repos:
        run = catalog.runs[repo]
        for row, score in enumerate(run['vectors'] @ vector):
            chunk = run['chunks'][row]
            ranked.append((float(score), repo, run['manifest']['run_id'], chunk['chunk_id'], row))
    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3]))
    results, seen = [], set()
    for score, repo, run_id, chunk_id, row in ranked:
        if chunk_id in seen:
            continue
        seen.add(chunk_id)
        run = catalog.runs[repo]
        chunk = run['chunks'][row]
        doc = run['documents'][chunk['document_id']]
        source = dict(source_id=f'S{len(results) + 1}', repo=repo, run_id=run_id,
                      commit_sha=doc['commit_sha'], document_id=doc['document_id'],
                      chunk_id=chunk_id, vector_row=row, score=score, path=doc['path'],
                      source_url=doc['source_url'])
        source.update({key: chunk[key] for key in
                       ('start_line', 'end_line', 'start_offset', 'end_offset', 'text')})
        results.append(source)
        if len(results) == top_k:
            break
    return results


class Retriever:
    def __init__(self, catalog):
        self.catalog = catalog
        self.tokenizer = load_tokenizer(E5_REVISION)
        self.model, self.device = load_encoder(E5_REVISION, 'auto')

    def measure_question(self, question):
        body = len(self.tokenizer(question, add_special_tokens=False, truncation=False,
                                  verbose=False)['input_ids'])
        final = len(self.tokenizer(QUERY_PREFIX + question, add_special_tokens=True,
                                   truncation=False, verbose=False)['input_ids'])
        if body > 500 or final > 512:
            raise APIError(422, 'QUESTION_TOO_LONG',
                           f'질문은 본문 500 tokens 이하, prefix 포함 512 tokens 이하여야 합니다. 현재 {body}/{final}.')
        return body

    def encode(self, question):
        import torch
        inputs = self.tokenizer(QUERY_PREFIX + question, return_tensors='pt',
                                truncation=False, verbose=False)
        if inputs['input_ids'].shape[-1] > 512:
            raise APIError(422, 'QUESTION_TOO_LONG', '최종 모델 입력이 512 tokens를 초과했습니다.')
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            pooled = masked_mean_pool(hidden, inputs['attention_mask'])
            vector = torch.nn.functional.normalize(pooled, p=2, dim=1)[0]
        return vector.cpu().numpy().astype(np.float32)

    def search(self, question, repos, top_k):
        started = time.perf_counter()
        count = self.measure_question(question)
        vector = self.encode(question)
        encoded = time.perf_counter()
        results = rank_sources(self.catalog, vector, repos, top_k)
        searched = time.perf_counter()
        timings = dict(query_encoding=(encoded-started)*1000, search=(searched-encoded)*1000,
                       context=0.0, generation=0.0, total=(searched-started)*1000)
        return dict(status='ok' if results else 'no_results', question_tokens=count,
                    results=results, timings_ms=timings)
