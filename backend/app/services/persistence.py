"""Validated records, DB re-read verification, and atomic active-run publication."""
import hashlib
import re
from datetime import datetime
import numpy as np
from sqlalchemy import select, update
from .storage import validate_records, stable_id
from .config import validate_path, CHUNKING
from .repositories import ensure_profile, now
from ..ml.encoder import PROFILE_ID, PROFILE, REVISION


def counts_for(files, documents, chunks, vectors):
    return dict(listed_files=len(files),collected_files=sum(r['status']=='collected' for r in files),
        excluded_files=sum(r['status']=='excluded' for r in files),failed_files=sum(r['status']=='failed' for r in files),
        documents=len(documents),chunks=len(chunks),vectors=0 if vectors is None else len(vectors))


def verify(files, documents, chunks, vectors, original, tokenizer=None):
    if original['embedding'].get('revision') != REVISION or original['embedding'].get('model_id') != PROFILE['model_id']:
        raise ValueError('Unsupported embedding profile')
    if original['chunking'] != CHUNKING:
        raise ValueError('Unsupported chunking settings')
    if original['status'] in ('completed','collected_only'):
        for key,expected in dict(prefix='passage: ',pooling='masked_mean',normalize=True,dimension=384,dtype='float32').items():
            if original['embedding'].get(key)!=expected:
                raise ValueError('Embedding configuration mismatch: '+key)
    for row in files:
        validate_path(row['path'])
        if not re.fullmatch('[a-f0-9]{40}',row['git_object_sha']):
            raise ValueError('Invalid Git object SHA')
        if row['status'] != 'collected' and not row['reason_code']:
            raise ValueError('Missing exclusion/error reason')
    for doc in documents:
        data = doc['text'].encode('utf-8')
        if '\x00' in doc['text'] or doc['blob_sha'] != hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest():
            raise ValueError('Git blob content hash mismatch')
    validate_records(files,documents,chunks,vectors,original,tokenizer)
    if original['status']=='completed' and any(c['embedding_status']!='embedded' for c in chunks):
        raise ValueError('Completed run contains unembedded chunks')


def bulk(conn, table, records, run_id):
    columns = set(table.c.keys())
    for start in range(0,len(records),200):
        rows = [{k:v for k,v in row.items() if k in columns} | dict(run_id=run_id)
                for row in records[start:start+200]]
        conn.execute(table.insert(),rows)


def save(db, repo_id, original, files, documents, chunks, vectors, *, importing=False, job_id=None, source_manifest=None):
    verify(files,documents,chunks,vectors,original)
    run_id = original['run_id']
    started = datetime.fromisoformat(original['started_at']) if original.get('started_at') else None
    finished = datetime.fromisoformat(original['finished_at']) if original.get('finished_at') else now()
    values = dict(repo_id=repo_id,profile_id=PROFILE_ID,schema_version='2.0',mode=original['mode'],
        status=original['status'],ref_requested=original.get('ref_requested'),commit_sha=original.get('commit_sha'),
        tree_sha=original.get('tree_sha'),scope_path=original.get('scope_path',''),inventory_complete=original['inventory_complete'],
        auth_used=original.get('auth_used',False),filter_settings=original['filter'],chunking_settings=original['chunking'],
        counts=original['counts'],timings_sec=original['timings_sec'],errors=original['errors'],libraries=original['libraries'],
        execution_settings=dict(batch_size=original['embedding'].get('batch_size',8),
            requested_device=original['embedding'].get('requested_device'),device=original['embedding'].get('device'),
            encoder_loaded=original['embedding'].get('encoder_loaded',False)),
        source_manifest=(source_manifest or original) if importing else None,started_at=started,finished_at=finished)
    with db.engine.begin() as conn:
        repos = db.tables['repositories']
        repo = conn.execute(select(repos).where(repos.c.repo_id==repo_id).with_for_update()).mappings().one()
        ensure_profile(db,conn)
        runs = db.tables['ingestion_runs']
        if importing:
            existing=conn.execute(select(runs).where(runs.c.run_id==run_id)).mappings().first()
            if existing:
                if existing['repo_id']!=repo_id or existing['source_manifest']!=(source_manifest or original):
                    raise ValueError('Existing run_id conflicts with source manifest')
                for name,expected_records,key in [('files',files,'path'),('documents',documents,'document_id'),('chunks',chunks,'chunk_id')]:
                    table=db.tables[name]
                    actual={r[key]:r for r in conn.execute(select(table).where(table.c.run_id==run_id)).mappings()}
                    if len(actual)!=len(expected_records):raise ValueError('Existing run row count mismatch')
                    for record in expected_records:
                        old=actual.get(record[key])
                        if old is None or any(old[k]!=v for k,v in record.items() if k in table.c.keys() and k!='run_id'):
                            raise ValueError('Existing run records conflict with import')
                table=db.tables['embeddings']
                actual={r['chunk_id']:r['embedding'] for r in conn.execute(select(table).where(table.c.run_id==run_id)).mappings()}
                expected={c['chunk_id']:vectors[c['vector_row']] for c in chunks if c['embedding_status']=='embedded'}
                if actual.keys()!=expected.keys() or any(not np.array_equal(np.asarray(actual[k],dtype=np.float32),v) for k,v in expected.items()):
                    raise ValueError('Existing run vectors conflict with import')
                return False
            conn.execute(runs.insert().values(run_id=run_id,created_at=started or finished,**values))
        else:
            conn.execute(update(runs).where(runs.c.run_id==run_id).values(**values))
        for name,records in [('files',files),('documents',documents),('chunks',chunks)]:
            bulk(conn,db.tables[name],records,run_id)
        embeddings = [dict(chunk_id=c['chunk_id'],profile_id=PROFILE_ID,embedding=vectors[c['vector_row']])
                      for c in chunks if c['embedding_status']=='embedded']
        bulk(conn,db.tables['embeddings'],embeddings,run_id)
        # Re-read the committed representation before publishing; checks also catch adapter loss.
        stored = {}
        for name in ('files','documents','chunks','embeddings'):
            table = db.tables[name]
            stored[name] = [dict(r) for r in conn.execute(select(table).where(table.c.run_id==run_id)).mappings()]
        docs = {r['document_id']:r for r in stored['documents']}
        chunks_by_doc = {key:[] for key in docs}
        vector_map = {r['chunk_id']:r['embedding'] for r in stored['embeddings']}
        for c in stored['chunks']:
            chunks_by_doc[c['document_id']].append(c)
        reconstructed_vectors=[]
        for c in stored['chunks']:
            c['vector_row']=None
            if c['chunk_id'] in vector_map:
                c['vector_row']=len(reconstructed_vectors)
                reconstructed_vectors.append(vector_map[c['chunk_id']])
        for d in docs.values():
            d.update(repo=original['repo'],commit_sha=original['commit_sha'])
        doc_by_path={d['path']:d for d in docs.values()}
        for f in stored['files']:
            d=doc_by_path.get(f['path']); cs=chunks_by_doc[d['document_id']] if d else []
            f.update(document_id=d['document_id'] if d else None,chunk_count=len(cs),
                     embedded_chunk_count=sum(c['embedding_status']=='embedded' for c in cs))
        array=np.asarray(reconstructed_vectors,dtype=np.float32).reshape(-1,384) if vectors is not None else None
        verify(stored['files'],list(docs.values()),stored['chunks'],array,original)
        if original['status']=='completed' and original['mode']=='embed' and len(embeddings)>0:
            old = conn.execute(select(runs.c.created_at).where(runs.c.run_id==repo['active_run_id'])).scalar_one_or_none() if repo['active_run_id'] else None
            new = conn.execute(select(runs.c.created_at).where(runs.c.run_id==run_id)).scalar_one()
            if old is None or new>=old:
                conn.execute(update(repos).where(repos.c.repo_id==repo_id).values(active_run_id=run_id))
        if job_id:
            jobs=db.tables['jobs']; complete=original['status']=='completed'
            conn.execute(update(jobs).where(jobs.c.job_id==job_id).values(status='completed' if complete else 'failed',
                phase='finished',progress_unit='steps',completed_units=1,total_units=1,finished_at=finished,
                error=None if complete else dict(code='INGESTION_INCOMPLETE',message='수집 결과의 오류를 확인하세요.')))
    return True
