"""Real PostgreSQL integration checks in a disposable, explicitly isolated Compose project."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))


def check(condition,message):
    if not condition:raise AssertionError(message)


class FakeTokenizer:
    def __call__(self,text,**kwargs):return {'input_ids':list(range(len(text)))}


class FakeEncoder:
    device='cpu'
    tokenizer=FakeTokenizer()
    tokenizer_lock=threading.Lock()
    def question_tokens(self,q):
        from app.errors import APIError
        if len(q)>500:raise APIError(422,'QUESTION_TOO_LONG','too long')
        return len(q)
    def encode(self,texts,prefix):
        result=np.zeros((len(texts),384),dtype=np.float32);result[:,0]=1
        return result


class FakeGenerator:
    dtype='float32'
    def answer(self,question,sources):
        return dict(answer_status='generated',answer='근거 확인 [S1]',context_source_ids=['S1'],
            cited_source_ids=['S1'],invalid_citation_ids=[],generation=dict(model_id='fake',revision='0'*40,
                prompt_version='1.1',input_tokens=1,output_tokens=1,finish_reason='eos')),[],dict(context=0,generation=0)


def tests(db,settings):
    from fastapi.testclient import TestClient
    from sqlalchemy import text,select,update
    from sqlalchemy.exc import IntegrityError
    from app.main import create_app
    from app.jobs import Jobs
    from app.request_logging import RequestLogger
    from app.ml.encoder import PROFILE_ID,PROFILE
    from app.services.storage import stable_id
    from app.services.repositories import now
    from manage import import_output
    report=import_output(db,ROOT/'tests/repo_ingestion/output')
    check(not report['failures'],str(report))
    report2=import_output(db,ROOT/'tests/repo_ingestion/output')
    check(report2['imported']==0 and report2['skipped']==report['imported'],'idempotent legacy import: '+str(report2))
    print(f'PASS: all {report["imported"]} legacy runs imported; repeat import skips unchanged runs',flush=True)
    lock=threading.Lock();encoder=FakeEncoder();generator=FakeGenerator()
    jobs=Jobs(db,settings,encoder,lock);jobs.start()
    runtime=dict(db=db,settings=settings,encoder=encoder,generator=generator,jobs=jobs,
                 inference_lock=lock,request_logger=RequestLogger(ROOT/'data/checks/requests.jsonl'))
    try:
        with TestClient(create_app(runtime=runtime),raise_server_exceptions=False) as client:
            response=client.get('/api/v1/repos');check(response.status_code==200,response.text)
            repos=response.json()['data']['items'];check(len(repos)==2 and all(r['searchable'] for r in repos),'legacy active runs')
            small=next(r for r in repos if r['repo']=='octocat/hello-world')
            big=next(r for r in repos if r['repo']=='hurwan0629/runstop')
            latest=next(json.loads(p.read_text(encoding='utf-8')) for p in (ROOT/'tests/repo_ingestion/output').glob('*/*/manifest.json') if p.parent.name==big['active_run_id'])
            check(big['counts']==latest['counts'],'latest legacy counts')
            run=client.get('/api/v1/runs/'+big['active_run_id']);check(run.status_code==200,run.text)
            check(run.json()['data']['schema_version']=='2.0' and 'source_manifest' not in run.json()['data'],'manifest projection')
            for status in ('collected','excluded'):
                response=client.get(f"/api/v1/runs/{big['active_run_id']}/files",params={'status':status,'limit':2})
                check(response.status_code==200,response.text)
                check(len(response.json()['data']['items'])==2,'file pagination')
            response=client.get(f"/api/v1/runs/{big['active_run_id']}/files",params={'path_prefix':'docs','limit':2})
            check(response.status_code==200 and all(r['path'].startswith('docs/') for r in response.json()['data']['items']),'path prefix')
            body=dict(question='hello',repo_ids=[small['repo_id']],top_k=5)
            response=client.post('/api/v1/search',json=body);check(response.status_code==200,response.text)
            source=response.json()['data']['sources'][0]
            response=client.get(f"/api/v1/documents/{source['run_id']}/{source['document_id']}")
            check(response.status_code==200,response.text);doc=response.json()['data']
            check(source['text']==doc['text'][source['start_offset']:source['end_offset']],'source reverse lookup')
            response=client.post('/api/v1/answers',json=body);check(response.status_code==200,response.text)
            check(response.json()['data']['cited_source_ids']==['S1'],'answer citations')
            lock.acquire()
            try:
                check(client.post('/api/v1/search',json=body).status_code==409,'busy response')
                check(client.get('/api/v1/health').status_code==200,'GET remains responsive while busy')
            finally:lock.release()
            check(client.post('/api/v1/search',json=body|{'question':'x'*501}).status_code==422,'server token limit')
            check(client.post('/api/v1/repos',json={'repo_url':'https://github.com/octocat/Hello-World.git'}).status_code==200,'register existing')
            print('PASS: service routes, filtering, pagination, reverse lookup, busy guard, token limit',flush=True)
            # Attempt cross-run profile references and negative counters against the actual DB.
            with db.engine.connect() as conn:
                chunk=conn.execute(text('SELECT run_id,chunk_id FROM chunks LIMIT 1')).first()
            with db.engine.begin() as conn:
                profile=PROFILE|dict(revision='0'*40);other=stable_id(profile)
                conn.execute(db.tables['embedding_profiles'].insert().values(profile_id=other,**profile))
            rejected=0
            for statement,params in [
                ('INSERT INTO embeddings(run_id,chunk_id,profile_id,embedding) VALUES(:r,:c,:p,CAST(:v AS vector(384)))',dict(r=chunk.run_id,c=chunk.chunk_id,p=other,v='['+','.join(['1']+['0']*383)+']')),
                ('UPDATE repositories SET active_run_id=:run WHERE repo_id=:repo',dict(run=big['active_run_id'],repo=small['repo_id'])),
                ('UPDATE chunks SET end_offset=start_offset WHERE run_id=:r AND chunk_id=:c',dict(r=chunk.run_id,c=chunk.chunk_id)),
            ]:
                try:
                    with db.engine.begin() as conn:conn.execute(text(statement),params)
                except IntegrityError:rejected+=1
            check(rejected==3,'composite FK/CHECK constraints')
            print('PASS: actual DB rejects cross-profile, cross-repo, and invalid offset writes',flush=True)
            registered=client.post('/api/v1/repos',json={'repo_url':'https://github.com/check/local-mvp'}).json()['data']
            gate=threading.Event();entered=threading.Event()
            data='hello 😀\r\nservice test\n'.encode('utf-8')
            sha=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
            class FakeClient:
                def __init__(self,*args):pass
                def snapshot(self,ref):
                    if ref=='fail':raise ValueError('expected ingestion failure')
                    entered.set();gate.wait(10);return '1'*40,'2'*40
                def inventory(self,tree):return [dict(type='blob',mode='100644',path='README.md',sha=sha,size=len(data))],True,[]
                def blob(self,*args):return data
            def wait_job(jid):
                for _ in range(200):
                    response=client.get('/api/v1/jobs/'+jid);check(response.status_code==200,response.text)
                    j=response.json()['data']
                    if j['status'] not in ('queued','running'):return j
                    time.sleep(.05)
                raise AssertionError('job did not terminate')
            with patch('app.services.ingestion.GitHubClient',FakeClient):
                response=client.post(f"/api/v1/repos/{registered['repo_id']}/ingestions",json={});check(response.status_code==202,response.text)
                first=response.json()['data'];check(entered.wait(5),'worker not started')
                check(client.post(f"/api/v1/repos/{registered['repo_id']}/ingestions",json={}).status_code==409,'duplicate job')
                gate.set();complete=wait_job(first['job_id']);check(complete['status']=='completed',str(complete))
                active=next(r for r in client.get('/api/v1/repos').json()['data']['items'] if r['repo_id']==registered['repo_id'])
                check(active['active_run_id']==first['run_id'] and active['counts']['vectors']==1,'new run activation')
                failed=client.post(f"/api/v1/repos/{registered['repo_id']}/ingestions",json={'ref':'fail'}).json()['data']
                check(wait_job(failed['job_id'])['status']=='failed','failed job')
                active=next(r for r in client.get('/api/v1/repos').json()['data']['items'] if r['repo_id']==registered['repo_id'])
                check(active['active_run_id']==first['run_id'],'failed run must preserve previous active run')
            print('PASS: background ingestion, progress, duplicate guard, atomic activation, failure preserves old run',flush=True)
    finally:jobs.stop()
    # Recovery handles a crash, and queued work remains resumable.
    with db.engine.begin() as conn:
        job=db.tables['jobs'];run=db.tables['ingestion_runs']
        j=conn.execute(select(job).where(job.c.status=='failed')).mappings().first()
        conn.execute(update(job).where(job.c.job_id==j['job_id']).values(status='running',phase='listing',finished_at=None,error=None))
        conn.execute(update(run).where(run.c.run_id==j['run_id']).values(status='running',finished_at=None,errors=[]))
    recovered=Jobs(db,settings,encoder,lock);recovered.start();recovered.stop()
    with db.engine.connect() as conn:
        check(conn.execute(select(job.c.status).where(job.c.job_id==j['job_id'])).scalar_one()=='interrupted','startup recovery')
    print('PASS: startup converts interrupted running job into explicit terminal state',flush=True)


def model_tests(db,settings,serve=False):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.jobs import Jobs
    from app.ml.encoder import Encoder
    from app.ml.generator import Generator
    from app.request_logging import RequestLogger
    print('Loading real cached E5 and Qwen for model checks...',flush=True)
    encoder=Encoder(settings);generator=Generator(settings);lock=threading.Lock()
    jobs=Jobs(db,settings,encoder,lock);jobs.start()
    runtime=dict(db=db,settings=settings,encoder=encoder,generator=generator,jobs=jobs,
        inference_lock=lock,request_logger=RequestLogger(ROOT/'data/checks/model-requests.jsonl'))
    try:
        with TestClient(create_app(runtime=runtime),raise_server_exceptions=False) as client:
            repos=client.get('/api/v1/repos').json()['data']['items']
            repo=next(r for r in repos if r['repo']=='octocat/hello-world')
            body=dict(question='What does the README say?',repo_ids=[repo['repo_id']],top_k=3)
            response=client.post('/api/v1/search',json=body);check(response.status_code==200,response.text)
            check(response.json()['data']['sources'][0]['path']=='README','real E5 retrieval')
            response=client.post('/api/v1/answers',json=body);check(response.status_code==200,response.text)
            answer=response.json()['data'];check(bool(answer['answer']) and answer['generation']['output_tokens']>0,'real Qwen generation')
            check(not answer['invalid_citation_ids'],'real citation bounds')
            (ROOT/'data/checks').mkdir(parents=True,exist_ok=True)
            (ROOT/'data/checks/model-result.json').write_text(json.dumps(answer,ensure_ascii=False,indent=2),encoding='utf-8')
            print(f'PASS: real E5 retrieval + Qwen generation on {encoder.device}; {answer["generation"]["output_tokens"]} output tokens',flush=True)
        if serve:
            import uvicorn
            print('Browser check server: http://127.0.0.1:8000 (disposable DB)',flush=True)
            uvicorn.run(create_app(runtime=runtime),host='127.0.0.1',port=8000,workers=1)
    finally:jobs.stop()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--models',action='store_true')
    parser.add_argument('--serve',action='store_true',help='Keep the model check app open for browser checks until Ctrl+C')
    args=parser.parse_args()
    project='git-rag-service-check-'+uuid4().hex[:12]
    env=os.environ.copy();env.update(COMPOSE_PROJECT_NAME=project,POSTGRES_DB='git_rag_check',POSTGRES_USER='git_rag_check',
        POSTGRES_PASSWORD=secrets.token_hex(24),POSTGRES_PORT='0',POSTGRES_HOST='127.0.0.1')
    compose=['docker','compose','--env-file',str(ROOT/'.env.example'),'-f',str(ROOT/'compose.yaml'),'--project-name',project]
    def run(argv,check=True):
        result=subprocess.run(argv,cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
        if check and result.returncode:raise RuntimeError('Command failed: '+str(argv[:3]))
        return result
    db=None
    try:
        config=json.loads(run(compose+['config','--format','json']).stdout)
        check(config['volumes']['postgres_data']['name']==project+'_postgres_data','test volume isolation')
        run(compose+['up','-d','--wait','--wait-timeout','60','db'])
        state=json.loads(run(compose+['ps','--format','json','db']).stdout)
        if isinstance(state,list):state=state[0]
        env['POSTGRES_PORT']=str(next(p['PublishedPort'] for p in state['Publishers'] if p['TargetPort']==5432))
        for key in ('POSTGRES_DB','POSTGRES_USER','POSTGRES_PASSWORD','POSTGRES_PORT','POSTGRES_HOST'):os.environ[key]=env[key]
        from manage import migrate
        from app.config import Settings
        from app.db import Database
        settings=Settings();migrate();migrate();db=Database(settings.database_url)
        print('PASS: Alembic migration, including repeat upgrade, on PostgreSQL 17',flush=True)
        tests(db,settings)
        if args.models:model_tests(db,settings,args.serve)
    finally:
        if db:db.close()
        run(compose+['down','--volumes'])
        check(run(['docker','volume','inspect',project+'_postgres_data'],check=False).returncode!=0,'test cleanup')
        print('PASS: disposable service database removed; root .env credentials were not used',flush=True)


if __name__=='__main__':main()
