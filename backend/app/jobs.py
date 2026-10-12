import threading
import time
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from .errors import APIError
from .services.repositories import ensure_profile, now
from .services.config import CHUNKING, DEFAULT_EXTENSIONS, filter_settings
from .ml.encoder import PROFILE_ID


class Jobs:
    def __init__(self, db, settings, encoder, inference_lock):
        self.db,self.settings,self.encoder,self.inference_lock=db,settings,encoder,inference_lock
        self.wakeup=threading.Event()
        self.stopping=threading.Event()
        self.thread=None
        self.lease=None

    def start(self):
        self.lease=self.db.engine.connect()
        if not self.lease.execute(text('SELECT pg_try_advisory_lock(671902026)')).scalar_one():
            self.lease.close()
            raise ValueError('Another service job runner is active; use one API process')
        self.lease.commit()
        with self.db.engine.begin() as conn:
            jobs=self.db.tables['jobs']; runs=self.db.tables['ingestion_runs']
            interrupted=list(conn.execute(select(jobs.c.run_id).where(jobs.c.status=='running')).scalars())
            error=dict(code='PROCESS_INTERRUPTED',message='프로세스 종료로 중단되었습니다. 새 수집을 시작하세요.')
            conn.execute(update(jobs).where(jobs.c.status=='running').values(status='interrupted',phase='finished',finished_at=now(),error=error))
            if interrupted:
                conn.execute(update(runs).where(
                    runs.c.run_id.in_(interrupted)
                ).values(status='failed',finished_at=now(),errors=[dict(stage='startup',path=None,**error)]))
        self.thread=threading.Thread(target=self.loop,name='ingestion-worker',daemon=False)
        self.thread.start()

    def stop(self):
        self.stopping.set(); self.wakeup.set()
        if self.thread:
            self.thread.join()
        if self.lease:
            self.lease.execute(text('SELECT pg_advisory_unlock(671902026)')); self.lease.commit(); self.lease.close()

    def submit(self, repo_id, request):
        run_id=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ_')+uuid4().hex
        job_id=uuid4()
        jobs,runs,repos=(self.db.tables[n] for n in ('jobs','ingestion_runs','repositories'))
        from types import SimpleNamespace
        filters=filter_settings(SimpleNamespace(max_file_bytes=1000000,extensions=sorted(DEFAULT_EXTENSIONS)))
        try:
            with self.db.engine.begin() as conn:
                if not conn.execute(select(repos.c.repo_id).where(repos.c.repo_id==repo_id)).first():
                    raise APIError(404,'NOT_FOUND','repo를 찾을 수 없습니다.')
                ensure_profile(self.db,conn)

                conn.execute(runs.insert()
                  .values(run_id=run_id,repo_id=repo_id,profile_id=PROFILE_ID,schema_version='2.0',
                      mode='embed',status='pending',
                      ref_requested=request.ref,
                      scope_path=request.scope_path,
                      auth_used=bool(self.settings.github_token),
                      filter_settings=filters,chunking_settings=CHUNKING,
                      counts={},timings_sec={},
                      execution_settings=dict(
                          batch_size=8,requested_device=self.settings.device,
                          device=self.encoder.device,encoder_loaded=True
                      ),
                      errors=[],libraries={}
                ))
                
                conn.execute(jobs.insert().values(job_id=job_id,run_id=run_id,repo_id=repo_id,kind='ingestion',status='queued',phase='queued'))
        except IntegrityError:
            raise APIError(409,'INGESTION_ALREADY_ACTIVE','이 저장소의 수집이 이미 대기 또는 실행 중입니다.') from None
        self.wakeup.set()
        return dict(job_id=job_id,run_id=run_id,repo_id=repo_id,status='queued')

    def progress(self, job_id, phase, unit=None, current=0, total=None):
        if self.stopping.is_set():
            raise InterruptedError('Service shutdown requested')
        with self.db.engine.begin() as conn:
            jobs=self.db.tables['jobs']
            conn.execute(update(jobs).where(jobs.c.job_id==job_id)
              .values(phase=phase,progress_unit=unit,completed_units=current,total_units=total))

    def loop(self):
        from .services.ingestion import execute
        while not self.stopping.is_set():
            with self.db.engine.begin() as conn:
                jobs=self.db.tables['jobs']; runs=self.db.tables['ingestion_runs']
                job=conn.execute(select(jobs).where(jobs.c.status=='queued').order_by(jobs.c.created_at,jobs.c.job_id).limit(1)).mappings().first()
                if job:
                    conn.execute(update(jobs).where(jobs.c.job_id==job['job_id']).values(status='running',phase='listing',started_at=now()))
                    conn.execute(update(runs).where(runs.c.run_id==job['run_id']).values(status='running',started_at=now()))
            if not job:
                self.wakeup.wait(1); self.wakeup.clear(); continue
            try:
                # 실제로 job의 작업
                execute(self,dict(job))
            except Exception as error:
                from .config import redact
                from sqlalchemy.exc import SQLAlchemyError
                message='DB 저장 또는 검증에 실패했습니다.' if isinstance(error,SQLAlchemyError) else redact(error)
                detail=dict(code='PROCESS_INTERRUPTED' if isinstance(error,InterruptedError) else 'INGESTION_FAILED',message=message)
                with self.db.engine.begin() as conn:
                    conn.execute(update(jobs).where(jobs.c.job_id==job['job_id']).values(status='interrupted' if isinstance(error,InterruptedError) else 'failed',phase='finished',finished_at=now(),error=detail))
                    conn.execute(update(runs).where(runs.c.run_id==job['run_id']).values(status='failed',finished_at=now(),errors=[dict(stage='worker',path=None,**detail)]))
