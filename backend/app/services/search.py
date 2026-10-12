import time
from urllib.parse import quote
from sqlalchemy import text
from ..errors import APIError
from ..ml.encoder import PROFILE_ID
from .repositories import searchable_run


def source_url(repo,commit,path):
    return f'https://github.com/{repo}/blob/{commit}/{quote(path,safe="/")}'


def search(db, encoder, request):
    started=time.perf_counter()
    with db.engine.connect() as conn:
        rows=list(conn.execute(text('''SELECT r.repo_id,r.repo,r.active_run_id AS run_id,
            i.commit_sha,i.status,i.mode,i.profile_id,i.counts
            FROM repositories r LEFT JOIN ingestion_runs i ON i.run_id=r.active_run_id
            WHERE r.repo_id=ANY(:ids) ORDER BY r.repo_id'''),dict(ids=request.repo_ids)).mappings())
    if len(rows)!=len(request.repo_ids):
        raise APIError(404,'NOT_FOUND','선택한 repo를 찾을 수 없습니다.')
    if any(not searchable_run(r) for r in rows):
        raise APIError(409,'REPO_NOT_SEARCHABLE','선택한 repo에 호환되는 활성 벡터가 없습니다.')
    tokens=encoder.question_tokens(request.question)
    encoded=time.perf_counter()
    vector=encoder.encode([request.question],'query: ')[0]
    vector_text='['+','.join(str(float(v)) for v in vector)+']'
    query_done=time.perf_counter()
    with db.engine.connect() as conn:
        sources=[dict(r) for r in conn.execute(text('''SELECT r.repo_id,r.repo,i.run_id,i.commit_sha,
            c.document_id,c.chunk_id,d.path,c.start_line,c.end_line,c.start_offset,c.end_offset,c.text,
            1-(e.embedding <=> CAST(:vector AS vector(384))) AS score
            FROM embeddings e JOIN chunks c USING(run_id,chunk_id)
            JOIN documents d USING(run_id,document_id)
            JOIN ingestion_runs i ON i.run_id=e.run_id JOIN repositories r ON r.repo_id=i.repo_id
            WHERE e.run_id=ANY(:runs) AND e.profile_id=:profile
            ORDER BY e.embedding <=> CAST(:vector AS vector(384)),e.run_id,e.chunk_id LIMIT :top'''),
            dict(vector=vector_text,runs=[r['run_id'] for r in rows],profile=PROFILE_ID,top=request.top_k)).mappings()]
    done=time.perf_counter()
    for index,s in enumerate(sources):
        s.update(source_id='S'+str(index+1),source_url=source_url(s['repo'],s['commit_sha'],s['path']))
    return dict(question=request.question,question_tokens=tokens,
        selected_runs=[{k:r[k] for k in ('repo_id','run_id','commit_sha')} for r in rows],sources=sources,
        timings_ms=dict(query_encoding=(query_done-encoded)*1000,search=(done-query_done)*1000,
                        context=0,generation=0,total=(done-started)*1000))
