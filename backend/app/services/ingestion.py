import sys
import time
import importlib.metadata
import numpy as np
from sqlalchemy import select
from .github_client import GitHubClient
from .collector import collect
from .chunker import chunk_document
from .persistence import counts_for, save, verify
from .repositories import now
from ..config import redact
from ..ml.encoder import MODEL_ID, REVISION


def execute(runner, job):
    db=runner.db; run_id=job['run_id']; jid=job['job_id']
    with db.engine.connect() as conn:
        runs,repos=(db.tables[n] for n in ('ingestion_runs','repositories'))
        run=dict(conn.execute(select(runs).where(runs.c.run_id==run_id)).mappings().one())
        repo=conn.execute(select(repos).where(repos.c.repo_id==job['repo_id'])).mappings().one()['repo']
    # ========================================
    original=dict(
        run_id=run_id,
        repo=repo,repo_url='https://github.com/'+repo,mode='embed',status='running',
        ref_requested=run['ref_requested'],scope_path=run['scope_path'],commit_sha=None,tree_sha=None,
        started_at=run['started_at'].isoformat(),finished_at=None,inventory_complete=False,
        auth_used=bool(runner.settings.github_token),
        filter=run['filter_settings'],
        chunking=run['chunking_settings'],
        embedding=dict(
            model_id=MODEL_ID,revision=REVISION,batch_size=8,device=runner.encoder.device,
            prefix='passage: ',pooling='masked_mean',normalize=True,dimension=384,dtype='float32',
            requested_device=runner.settings.device,encoder_loaded=True
        ),
        counts={},timings_sec={},errors=[],libraries={'python':sys.version.split()[0]})
    # ========================================
    for name in ('torch','transformers','numpy','sqlalchemy','pgvector'):
        original['libraries'][name]=importlib.metadata.version(name)
    files=[]; documents=[]; chunks=[]; vectors=[]
    stage='listing'; started=time.perf_counter(); stage_started=started
    try:
        # 수집 대상 존재 확인
        client=GitHubClient(repo,runner.settings.github_token)
        commit,tree=client.snapshot(run['ref_requested'])
        original.update(commit_sha=commit,tree_sha=tree)
        entries,complete,errors=client.inventory(tree)
        original.update(inventory_complete=complete)
        original['errors'].extend(errors)
        if run['scope_path'] and complete and not any(e['path']==run['scope_path'] and e['type']=='tree' for e in entries):
            raise ValueError('scope_path에 해당하는 폴더가 없습니다.')

        # 수집 시작
        original['timings_sec'][stage]=time.perf_counter()-stage_started
        stage='collecting';stage_started=time.perf_counter()
        total=sum(e['type']!='tree' for e in entries)
        runner.progress(jid,stage,'files',0,total)
        files,documents=collect(client,entries,repo,commit,run_id,run['scope_path'],original['filter'],
            progress=lambda current:runner.progress(jid,stage,'files',current,total))
        original['timings_sec'][stage]=time.perf_counter()-stage_started

        # chunking 시작
        stage='chunking';stage_started=time.perf_counter()
        runner.progress(jid,stage,'files',0,len(documents))
        for index,doc in enumerate(documents):
            try:
                with runner.encoder.tokenizer_lock:
                    chunks.extend(chunk_document(doc,runner.encoder.tokenizer,REVISION))
            except Exception as error:
                original['errors'].append(dict(stage=stage,path=doc['path'],code='chunking_failed',message=redact(error)))
            runner.progress(jid,stage,'files',index+1,len(documents))
        original['timings_sec'][stage]=time.perf_counter()-stage_started

        # embedding 시작
        stage='embedding';stage_started=time.perf_counter()
        runner.progress(jid,stage,'chunks',0,len(chunks))
        for start in range(0,len(chunks),8):
            batch=chunks[start:start+8]
            try:
                with runner.inference_lock:
                    result=runner.encoder.encode([c['text'] for c in batch],'passage: ')
                for c,v in zip(batch,result):
                    c.update(embedding_status='embedded',vector_row=len(vectors)); vectors.append(v)
            except Exception as error:
                for c in batch:
                    c.update(embedding_status='failed',embedding_error=redact(error),vector_row=None)
                original['errors'].append(dict(stage=stage,path=None,code='embedding_failed',message=redact(error)))
            runner.progress(jid,stage,'chunks',start+len(batch),len(chunks))
        original['timings_sec'][stage]=time.perf_counter()-stage_started

    except InterruptedError:
        raise
    except Exception as error:
        original['errors'].append(dict(stage=stage,path=None,code=getattr(error,'code','ingestion_failed'),message=redact(error)))
    
    # 작업 종료 및 저장 시작
    array=np.asarray(vectors,dtype=np.float32).reshape(-1,384)
    grouped={doc['document_id']:[] for doc in documents}
    for c in chunks:
        grouped[c['document_id']].append(c)
    for row in files: 
        cs=grouped.get(row['document_id'],[])
        row.update(chunk_count=len(cs),embedded_chunk_count=sum(c['embedding_status']=='embedded' for c in cs))
    original['counts']=counts_for(files,documents,chunks,array)
    failed=bool(original['errors']) or original['counts']['failed_files']>0 or not original['inventory_complete']
    original['status']=('partial' if documents else 'failed') if failed else 'completed'
    original['finished_at']=now().isoformat()
    original['timings_sec']['total']=time.perf_counter()-started
    runner.progress(jid,'validating','steps',0,1)
    verify(files,documents,chunks,array,original)
    runner.progress(jid,'publishing','steps',0,1)
    save(db,job['repo_id'],original,files,documents,chunks,array,job_id=jid)
