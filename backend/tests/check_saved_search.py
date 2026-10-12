"""Check real HTTP retrieval and document references against an already collected repository."""
import argparse
import json
from pathlib import Path
import sys
import requests

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from app.services.config import parse_repo_url


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo_url')
    parser.add_argument('--base-url',default='http://127.0.0.1:8000')
    args=parser.parse_args()
    repo=parse_repo_url(args.repo_url)
    base=args.base_url.rstrip('/')+'/api/v1'
    def call(method,path,body=None):
        response=requests.request(method,base+path,json=body,timeout=120)
        response.raise_for_status()
        return response.json()['data']
    saved=next((r for r in call('GET','/repos')['items'] if r['repo']==repo),None)
    if not saved or not saved['searchable']:raise AssertionError('Saved repository is not searchable')
    manifest=call('GET','/runs/'+saved['active_run_id'])
    questions=[
        ('OpenAI Python SDK에서 비동기 요청을 보내려면 AsyncOpenAI를 어떻게 사용하나요?','AsyncOpenAI'),
        ('How do I configure max_retries and request timeouts in the OpenAI Python client?','max_retries'),
        ('How does the OpenAI Python SDK handle APIConnectionError and RateLimitError?','APIConnectionError'),
    ]
    results=[]
    for question,term in questions:
        result=call('POST','/search',dict(question=question,repo_ids=[saved['repo_id']],top_k=5))
        if not result['sources']:raise AssertionError('No sources returned')
        for source in result['sources']:
            if source['repo']!=repo or source['run_id']!=saved['active_run_id']:
                raise AssertionError('Search escaped selected repository/run')
            doc=call('GET',f"/documents/{source['run_id']}/{source['document_id']}")
            if doc['text'][source['start_offset']:source['end_offset']]!=source['text']:
                raise AssertionError('Source does not match original document')
            chunk=next(c for c in doc['chunks'] if c['chunk_id']==source['chunk_id'])
            if chunk['embedding_status']!='embedded':raise AssertionError('Unembedded chunk returned')
        result['expected_term']=term
        result['expected_term_found']=any(term in source['text'] for source in result['sources'])
        results.append(result)
        print(json.dumps(dict(question=question,expected_term_found=result['expected_term_found'],
            sources=[dict(path=s['path'],score=round(s['score'],4)) for s in result['sources']]),ensure_ascii=False),flush=True)
    report=dict(repo=repo,run_id=saved['active_run_id'],status=manifest['status'],counts=manifest['counts'],results=results)
    output=ROOT/'data/checks/saved-search-result.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not all(r['expected_term_found'] for r in results):
        raise AssertionError('Expected evidence missing; see '+str(output))
    print('PASS: real retrieval, selected-run scope, embedded chunks and original document references')


if __name__=='__main__':main()
