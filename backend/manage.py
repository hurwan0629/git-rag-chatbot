"""Database migration, output import, stored run activation, and pinned model download."""
import argparse
import copy
import json
from pathlib import Path
import numpy as np
from alembic.config import Config
from alembic import command
from app.config import ROOT, Settings
from app.db import Database
from app.ml.encoder import MODEL_ID, REVISION, cached_source
from app.ml.generator import MODEL_ID as GENERATOR_ID, REVISION as GENERATOR_REVISION
from app.services.config import parse_repo_url
from app.services.repositories import register
from app.services.storage import validate_dataset
from app.services.persistence import save, activate_run
from app.schemas import RUN_PATTERN


def migrate():
    command.upgrade(Config(str(ROOT/'backend/alembic.ini')),'head')


def import_output(db,output):
    imported=0;skipped=0;failures=[]
    output=Path(output).resolve()
    if not output.is_dir():raise ValueError('Output directory does not exist')
    from transformers import AutoTokenizer
    tokenizer=AutoTokenizer.from_pretrained(cached_source(MODEL_ID,REVISION,Settings(database_required=False).cache),
        local_files_only=True,trust_remote_code=False,token=False)
    for path in sorted(output.glob('*/*/manifest.json')):
        try:
            if not path.resolve().is_relative_to(output):raise ValueError('Manifest escapes output directory')
            source=json.loads(path.read_text(encoding='utf-8'))
            if source['schema_version']!='1.0':raise ValueError('Expected schema 1.0')
            files,docs,chunks=validate_dataset(path.parent,source,tokenizer)
            vectors=np.load(path.parent/'vectors.npy',allow_pickle=False) if (path.parent/'vectors.npy').is_file() else None
            original=copy.deepcopy(source)
            if not original['embedding'].get('revision') and not docs and not chunks:
                original['embedding'].update(model_id=MODEL_ID,revision=REVISION)
            if original['status']=='running':
                raise ValueError('Import refuses an unfinished run')
            repo=parse_repo_url(original['repo_url'])
            if repo!=original['repo']:raise ValueError('Repo normalization mismatch')
            row,_=register(db,'https://github.com/'+repo)
            if save(db,row['repo_id'],original,files,docs,chunks,vectors,importing=True,source_manifest=source):
                imported+=1
            else:skipped+=1
        except Exception as error:
            from app.config import redact
            failures.append(dict(run_folder=path.parent.name,message=redact(error)))
    return dict(imported=imported,skipped=skipped,failures=failures)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('migrate',help='Apply Alembic migrations')
    importer=sub.add_parser('import-output',help='Validate and import existing run folders; safe to repeat')
    importer.add_argument('output',nargs='?',default=str(ROOT/'tests/repo_ingestion/output'))
    activate=sub.add_parser('activate-run',help='Validate stored data and select a completed/partial run for search')
    activate.add_argument('run_id')
    download=sub.add_parser('download-models',help='Download pinned revisions into the shared cache')
    download.add_argument('--encoder-only',action='store_true')
    args=parser.parse_args()
    if args.command=='activate-run':
        import re
        if not re.fullmatch(RUN_PATTERN,args.run_id):parser.error('Invalid run_id')
    if args.command=='download-models':
        from huggingface_hub import snapshot_download
        settings=Settings(database_required=False)
        models=[(MODEL_ID,REVISION)]
        if not args.encoder_only:models.append((GENERATOR_ID,GENERATOR_REVISION))
        for model,revision in models:
            snapshot_download(model,revision=revision,cache_dir=str(settings.cache),token=False)
            print('Cached '+model+' @ '+revision)
    elif args.command=='migrate':migrate();print('Migration complete')
    else:
        db=Database(Settings().database_url)
        try:
            if args.command=='activate-run':
                from transformers import AutoTokenizer
                tokenizer=AutoTokenizer.from_pretrained(cached_source(MODEL_ID,REVISION,Settings(database_required=False).cache),
                    local_files_only=True,trust_remote_code=False,token=False)
                report=activate_run(db,args.run_id,tokenizer)
            else:report=import_output(db,args.output)
            print(json.dumps(report,ensure_ascii=False,indent=2))
            if report.get('failures'):raise SystemExit(1)
        finally:db.close()


if __name__=='__main__':main()
