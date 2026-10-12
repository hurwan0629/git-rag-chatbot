from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from ..errors import APIError
from ..ml.encoder import PROFILE, PROFILE_ID


def now():
    return datetime.now(timezone.utc)


def searchable_run(run):
    return bool(run and run['status'] in ('completed','partial') and run['mode']=='embed'
                and run['profile_id']==PROFILE_ID and run['commit_sha']
                and run['counts'].get('vectors',0)>0)


def ensure_profile(db, conn):
    table = db.tables['embedding_profiles']
    conn.execute(insert(table).values(profile_id=PROFILE_ID, **PROFILE).on_conflict_do_nothing())
    row=conn.execute(select(table).where(table.c.profile_id==PROFILE_ID)).mappings().one()
    if any(row[key]!=value for key,value in PROFILE.items()):
        raise ValueError('Stored embedding profile differs from its immutable configuration')


def register(db, repo_url):
    repo = repo_url.removeprefix('https://github.com/')
    table = db.tables['repositories']
    with db.engine.begin() as conn:
        result = conn.execute(insert(table).values(repo_id=uuid4(),repo=repo,repo_url=repo_url)
            .on_conflict_do_nothing(index_elements=['repo']).returning(table.c.repo_id)).first()
        row = dict(conn.execute(select(table).where(table.c.repo==repo)).mappings().one())
    row['searchable'] = False
    return row, result is not None


def list_repos(db):
    with db.engine.connect() as conn:
        repos = [dict(r) for r in conn.execute(text('SELECT * FROM repositories ORDER BY repo')).mappings()]
        runs = [dict(r) for r in conn.execute(text('SELECT run_id,repo_id,profile_id,status,mode,commit_sha,counts,created_at FROM ingestion_runs ORDER BY created_at DESC,run_id DESC')).mappings()]
        jobs = {r.repo_id:r.job_id for r in conn.execute(text("SELECT repo_id,job_id FROM jobs WHERE status IN ('queued','running')"))}
    for repo in repos:
        history = [r for r in runs if r['repo_id']==repo['repo_id']]
        active = next((r for r in history if r['run_id']==repo['active_run_id']),None)
        searchable = searchable_run(active)
        reason = None if searchable else ('no_active_run' if not active else 'incompatible_or_empty_run')
        repo.update(searchable=searchable,commit_sha=active['commit_sha'] if active else None,
                    counts=active['counts'] if active else {},unavailable_reason=reason,
                    active_job_id=jobs.get(repo['repo_id']),runs=history)
    return repos


def manifest(db, run_id):
    with db.engine.connect() as conn:
        run = conn.execute(select(db.tables['ingestion_runs']).where(db.tables['ingestion_runs'].c.run_id==run_id)).mappings().first()
        if run is None:
            raise APIError(404,'NOT_FOUND','run을 찾을 수 없습니다.')
        run = dict(run)
        repo = conn.execute(select(db.tables['repositories']).where(db.tables['repositories'].c.repo_id==run['repo_id'])).mappings().one()
        profile = dict(conn.execute(select(db.tables['embedding_profiles']).where(db.tables['embedding_profiles'].c.profile_id==run['profile_id'])).mappings().one())
    profile.pop('created_at')
    run.pop('source_manifest')
    searchable = repo['active_run_id']==run_id and searchable_run(run)
    run.pop('profile_id')
    run.update(repo=repo['repo'],repo_url=repo['repo_url'],embedding=profile,
               active=repo['active_run_id']==run_id,
               searchable=searchable)
    run['filter'] = run.pop('filter_settings')
    run['chunking'] = run.pop('chunking_settings')
    run['execution'] = run.pop('execution_settings')
    return run
