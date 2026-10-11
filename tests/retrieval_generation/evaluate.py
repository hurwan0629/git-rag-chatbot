"""실행 중인 로컬 서버에서 고정 질문의 Hit@5와 답변을 기록한다."""

import argparse
import json
import sys
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'retrieval_generation'

from repo_ingestion.storage import write_json
from .server import BASE_DIR, port_number, utc_now


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=port_number, default=8000, help='실행 중인 로컬 서버 포트')
    parser.add_argument('--answer', action='store_true', help='검색 대신 답변 생성까지 수행')
    args = parser.parse_args(argv)
    cases = json.loads((BASE_DIR / 'fixtures/questions.json').read_text(encoding='utf-8'))
    records, hits, counted = [], 0, 0
    for case in cases:
        body = dict(question=case['question'], repos=case['repos'], top_k=5)
        endpoint = '/api/answer' if args.answer else '/api/search'
        request = Request(f'http://127.0.0.1:{args.port}' + endpoint,
                          data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=300) as response:
                data = json.load(response)
        except HTTPError as error:
            data = json.load(error)
        paths = list(dict.fromkeys(source['path'] for source in data.get('results', [])))
        hit = bool(set(paths) & set(case['expected_paths'])) if not case['expects_no_evidence'] else None
        if hit is not None:
            hits += hit
            counted += 1
        print(f"{case['case_id']}: {'HIT' if hit else ('근거 없음 평가' if hit is None else 'MISS')} · {paths[:3]}", flush=True)
        records.append(dict(case_id=case['case_id'], question=case['question'], expected_paths=case['expected_paths'],
                            expects_no_evidence=case['expects_no_evidence'], hit=hit, result_paths=paths,
                            request_id=data.get('request_id'), error=data.get('error'),
                            answer=data.get('answer'), generation=data.get('generation'),
                            cited_source_ids=data.get('cited_source_ids'), invalid_citation_ids=data.get('invalid_citation_ids'),
                            timings_ms=data.get('timings_ms')))
    result = dict(created_at=utc_now(), mode='answer' if args.answer else 'search',
                  hit_at_5=hits/counted if counted else None, hits=hits, questions=counted,
                  search_criteria_passed=hits >= 4 and not any(r['error'] for r in records),
                  generation_requires_manual_review=args.answer, cases=records)
    path = BASE_DIR / 'output' / ('evaluation_' + uuid.uuid4().hex + '.json')
    write_json(path, result)
    print(f'Hit@5: {hits}/{counted} · 기록: {path}', flush=True)
    if args.answer:
        print('답변의 근거 일치와 확인 불가 응답은 기록을 읽고 별도로 검토하세요.', flush=True)
    return 0 if result['search_criteria_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
