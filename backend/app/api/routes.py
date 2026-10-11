import re
import time
from typing import Annotated, Literal
from uuid import UUID
from fastapi import APIRouter, Request, Response, Path, Query
from sqlalchemy import select, text
from ..errors import APIError
from ..schemas import RepoRequest, IngestionRequest, SearchRequest, RUN_PATTERN, HASH_PATTERN
from ..services.repositories import register, list_repos, manifest
from ..services.search import search, source_url
from ..services.config import validate_path

router=APIRouter(prefix='/api/v1')
RunId=Annotated[str,Path(pattern=RUN_PATTERN)]
HashId=Annotated[str,Path(pattern=HASH_PATTERN)]


def ok(request,data):
    return dict(request_id=request.state.request_id,status='ok',data=data)


@router.get('/health')
def health(request:Request,response:Response):
    state=request.app.state
    try:
        with state.db.engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        db_ready=True
    except Exception:
        db_ready=False
    ready=db_ready and state.encoder is not None and (state.settings.search_only or state.generator is not None) and state.jobs.thread.is_alive()
    if not ready: response.status_code=503
    return ok(request,dict(ready=ready,mode='search_only' if state.settings.search_only else 'rag',
        database_ready=db_ready,encoder_ready=state.encoder is not None,generator_ready=state.generator is not None,
        job_runner_ready=state.jobs.thread.is_alive(),device=state.encoder.device,
        generator_dtype=state.generator.dtype if state.generator else None))


@router.post('/repos')
def create_repo(body:RepoRequest,request:Request,response:Response):
    row,created=register(request.app.state.db,body.repo_url)
    row=next(r for r in list_repos(request.app.state.db) if r['repo_id']==row['repo_id'])
    response.status_code=201 if created else 200
    return ok(request,row)


@router.get('/repos')
def repos(request:Request):
    return ok(request,dict(items=list_repos(request.app.state.db)))


@router.post('/repos/{repo_id}/ingestions',status_code=202)
def ingest(repo_id:UUID,body:IngestionRequest,request:Request):
    return ok(request,request.app.state.jobs.submit(repo_id,body))


@router.get('/jobs/{job_id}')
def job(job_id:UUID,request:Request):
    db=request.app.state.db; table=db.tables['jobs']
    with db.engine.connect() as conn:
        row=conn.execute(select(table).where(table.c.job_id==job_id)).mappings().first()
    if row is None: raise APIError(404,'NOT_FOUND','job을 찾을 수 없습니다.')
    row=dict(row); unit=row.pop('progress_unit'); current=row.pop('completed_units'); total=row.pop('total_units')
    row['progress']=dict(unit=unit,current=current,total=total,percent=100*current/total if total else None)
    return ok(request,row)


@router.get('/runs/{run_id}')
def run(run_id:RunId,request:Request):
    return ok(request,manifest(request.app.state.db,run_id))


@router.get('/runs/{run_id}/files')
def files(run_id:RunId,request:Request,status:Literal['collected','excluded','failed']|None=None,
          path_prefix:str='',limit:Annotated[int,Query(ge=1,le=500)]=100,offset:Annotated[int,Query(ge=0)]=0):
    try: validate_path(path_prefix,allow_empty=True)
    except ValueError as error: raise APIError(422,'VALIDATION_ERROR',str(error)) from None
    db=request.app.state.db
    manifest(db,run_id)
    where='f.run_id=:run'
    params=dict(run=run_id,limit=limit,offset=offset)
    if status:
        where+=' AND f.status=:status'; params['status']=status
    if path_prefix:
        where+=" AND starts_with(f.path,:prefix)";params['prefix']=path_prefix+'/'
    with db.engine.connect() as conn:
        total=conn.execute(text('SELECT count(*) FROM files f WHERE '+where),params).scalar_one()
        rows=[dict(r) for r in conn.execute(text('''SELECT f.*,d.document_id,
            COALESCE(c.chunk_count,0) AS chunk_count,COALESCE(c.embedded_chunk_count,0) AS embedded_chunk_count
            FROM files f LEFT JOIN documents d ON (d.run_id,d.path)=(f.run_id,f.path)
            LEFT JOIN (SELECT run_id,document_id,count(*) AS chunk_count,
                count(*) FILTER(WHERE embedding_status='embedded') AS embedded_chunk_count
                FROM chunks WHERE run_id=:run GROUP BY run_id,document_id) c ON c.run_id=f.run_id AND c.document_id=d.document_id
            WHERE '''+where+' ORDER BY f.path LIMIT :limit OFFSET :offset'),params).mappings()]
    return ok(request,dict(items=rows,total=total,limit=limit,offset=offset))


@router.get('/documents/{run_id}/{document_id}')
def document(run_id:RunId,document_id:HashId,request:Request):
    db=request.app.state.db; docs=db.tables['documents']; cs=db.tables['chunks']
    with db.engine.connect() as conn:
        row=conn.execute(select(docs).where(docs.c.run_id==run_id,docs.c.document_id==document_id)).mappings().first()
        if row is None: raise APIError(404,'NOT_FOUND','문서를 찾을 수 없습니다.')
        boundaries=[{k:v for k,v in r.items() if k not in ('text','embedding_error','run_id','document_id')}
            for r in conn.execute(select(cs).where(cs.c.run_id==run_id,cs.c.document_id==document_id).order_by(cs.c.chunk_index)).mappings()]
    m=manifest(db,run_id)
    return ok(request,dict(row,repo=m['repo'],commit_sha=m['commit_sha'],chunks=boundaries,
                          source_url=source_url(m['repo'],m['commit_sha'],row['path'])))


def infer(body,request,generate=False):
    state=request.app.state; started=time.perf_counter()
    if generate and state.generator is None:
        raise APIError(503,'GENERATION_DISABLED','검색 전용 모드입니다.')
    if not state.inference_lock.acquire(blocking=False):
        raise APIError(409,'BUSY','모델이 수집 또는 다른 추론을 수행 중입니다. 잠시 후 다시 시도하세요.')
    messages=[]
    try:
        result=search(state.db,state.encoder,body)
        if generate:
            if result['sources']:
                answer,messages,timings=state.generator.answer(body.question,result['sources'])
                result.update(answer);result['timings_ms'].update(timings)
            else:
                result.update(answer_status='no_evidence',answer='제공된 근거에서 확인할 수 없습니다.',
                    context_source_ids=[],cited_source_ids=[],invalid_citation_ids=[],generation=None)
        result['timings_ms']['total']=(time.perf_counter()-started)*1000
        state.request_logger.write(dict(request_id=request.state.request_id,kind='answer' if generate else 'search',
            question=body.question,messages=messages,response=result))
        return ok(request,result)
    except APIError:
        raise
    except Exception as error:
        import torch
        if isinstance(error,torch.OutOfMemoryError):
            if state.encoder.device=='cuda':torch.cuda.empty_cache()
            raise APIError(500,'GPU_OOM','모델 실행 메모리가 부족합니다. CPU 실행 설정 또는 배치 크기를 확인하세요.') from None
        raise
    finally:
        state.inference_lock.release()


@router.post('/search')
def search_route(body:SearchRequest,request:Request):
    return infer(body,request)


@router.post('/answers')
def answer_route(body:SearchRequest,request:Request):
    return infer(body,request,True)
