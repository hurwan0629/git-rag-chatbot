"""CodeSearchNet과 LCA를 읽어 고정 학습 쌍·평가 청크를 준비한다."""
import argparse
import ast
from collections import Counter
import gzip
import io
import json
from pathlib import Path, PurePosixPath
import random
import re
import textwrap
import zipfile
import requests
from common import (HERE, ROOT, digest, file_hash, load_config, load_tokenizer, output_path,
                    protocol_hash, read_jsonl, timestamp, write_json, write_jsonl)
from app.services.chunker import chunk_document
from app.services.config import validate_path

CODESEARCHNET_REPO='code-search-net/code_search_net'
CODESEARCHNET_REVISION='bd0cf261e357a3eb5c8fba490d23ec1a1cd59555'


def download(url,path,max_bytes=2_000_000_000):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_file():return path
    partial=path.with_suffix(path.suffix+'.part')
    print('Download:',url,flush=True)
    with requests.get(url,stream=True,timeout=(20,120)) as response:
        response.raise_for_status();size=0
        with partial.open('wb') as stream:
            for block in response.iter_content(1024*1024):
                size+=len(block)
                if size>max_bytes:raise ValueError('Archive exceeds configured download limit')
                stream.write(block)
    partial.replace(path)
    return path


def source_files(path):
    path=Path(path)
    return sorted(p for p in path.rglob('*') if p.is_file() and
                  (p.name.endswith('.jsonl') or p.name.endswith('.jsonl.gz') or p.suffix=='.parquet')) if path.is_dir() else [path]


def parquet_rows(path):
    import pyarrow.parquet as pq
    # train 전체를 한 번에 Python 객체로 만들지 않고 배치마다 읽는다.
    source=pq.ParquetFile(path)
    columns=['repository_name','func_path_in_repository','language','split_name',
             'func_documentation_string','whole_func_string'] if 'repository_name' in source.schema_arrow.names else None
    for batch in source.iter_batches(batch_size=256,columns=columns):
        yield from batch.to_pylist()


def normalize_source_row(row):
    if 'repository_name' not in row:return row
    partition=row.get('split_name')
    return dict(repo=row['repository_name'],path=row['func_path_in_repository'],
                language=row['language'],partition='valid' if partition=='validation' else partition,
                docstring=row['func_documentation_string'],code=row['whole_func_string'])


def source_rows(path,split):
    for file in source_files(path):
        if file.suffix=='.parquet':
            for row in parquet_rows(file):yield normalize_source_row(row)
        elif file.suffix=='.zip':
            with zipfile.ZipFile(file) as archive:
                names=sorted(n for n in archive.namelist() if f'/jsonl/{split}/' in '/'+n and n.endswith(('.jsonl','.jsonl.gz')))
                if not names:raise ValueError('CodeSearchNet ZIP에 split이 없습니다: '+split)
                for name in names:
                    with archive.open(name) as raw:
                        stream=gzip.GzipFile(fileobj=raw) if name.endswith('.gz') else raw
                        for line in stream:
                            if line.strip():yield json.loads(line)
        else:
            opener=gzip.open if file.name.endswith('.gz') else open
            with opener(file,'rt',encoding='utf-8') as stream:
                for line in stream:
                    if line.strip():yield json.loads(line)


def normalize(text):return ' '.join(text.split()).casefold()


def clean_code(code,query):
    tree=ast.parse(textwrap.dedent(code))
    for node in ast.walk(tree):
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            doc=ast.get_docstring(node)
            if doc and normalize(doc).startswith(normalize(query)):
                node.body=node.body[1:] or [ast.Pass()]
    return ast.unparse(tree),ast.dump(tree,include_attributes=False)


def pairs(source,split,config,tokenizer,excluded_repos,blocked_codes=None,blocked_queries=None):
    rng=random.Random(config['seed']);stats=Counter();sample=[];repos=set()
    codes=set();queries=set();target=config['data']['train_pairs' if split=='train' else 'validation_pairs']
    m=config['model']
    for row in source_rows(source,split):
        stats['read']+=1;repo=str(row.get('repo','')).lower();repos.add(repo)
        if row.get('language','python')!='python':stats['language']+=1;continue
        if row.get('partition',split) not in (split,'valid' if split=='valid' else 'train'):
            raise ValueError('입력 파일의 split과 partition 불일치')
        if not re.fullmatch('[a-z0-9_.-]+/[a-z0-9_.-]+',repo):stats['invalid_repo']+=1;continue
        if repo in excluded_repos:stats['excluded_repository']+=1;continue
        query=row.get('docstring')
        query=query.strip() if isinstance(query,str) else ''
        if not query:stats['empty_query']+=1;continue
        try:positive,canonical=clean_code(row.get('code',''),query)
        except (SyntaxError,ValueError,TypeError):stats['invalid_python']+=1;continue
        if not positive.strip():stats['empty_code']+=1;continue
        qh=digest(normalize(query));ch=digest(canonical)
        if qh in queries or ch in codes or ch in (blocked_codes or ()) or qh in (blocked_queries or ()):
            stats['duplicate_or_cross_split']+=1;continue
        if (len(tokenizer(m['query_prefix']+query,verbose=False)['input_ids'])>512 or
            len(tokenizer(m['passage_prefix']+positive,verbose=False)['input_ids'])>512):stats['too_long']+=1;continue
        codes.add(ch);queries.add(qh);stats['accepted']+=1
        record=dict(pair_id=digest([repo,row.get('path',''),query,canonical]),repo=repo,
            path=row.get('path',''),query=query,positive=positive,code_hash=ch,query_hash=qh)
        # 전체 입력을 순회하면서 균일 reservoir sampling. 파일 앞부분만 고르지 않는다.
        if len(sample)<target:sample.append(record)
        else:
            index=rng.randrange(stats['accepted'])
            if index<target:sample[index]=record
    if len(sample)!=target:raise ValueError(f'{split}: 필터 후 {len(sample)}쌍, 요청 {target}쌍. config의 수를 줄이거나 입력을 확인하세요.')
    sample.sort(key=lambda r:r['pair_id'])
    return sample,dict(stats),repos,codes,queries


def lca_rows(path):
    path=Path(path)
    if path.suffix=='.parquet':
        import pyarrow.parquet as pq
        return pq.read_table(path).to_pylist()
    return read_jsonl(path)


def repo_name(row):
    repo=(str(row['repo_owner'])+'/'+str(row['repo_name'])).lower()
    if not re.fullmatch('[a-z0-9_.-]+/[a-z0-9_.-]+',repo):raise ValueError('Invalid repository')
    return repo


def snapshot_files(folder,repo,sha,allow_download,config):
    if not re.fullmatch('[a-f0-9]{40}',sha):raise ValueError('Invalid base_sha')
    repo_path=folder/repo.replace('/','__')
    directory=repo_path/sha
    max_bytes=config['evaluation']['max_file_bytes']
    if directory.is_dir():
        for path in sorted(directory.rglob('*.py')):
            if path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):continue
            if path.stat().st_size>max_bytes:continue
            try:text=path.read_bytes().decode('utf-8')
            except UnicodeError:continue
            if '\x00' not in text:yield path.relative_to(directory).as_posix(),text
        return
    archive_path=repo_path/(sha+'.zip')
    if not archive_path.is_file():
        if not allow_download:raise ValueError('Snapshot missing: '+str(archive_path))
        download(f'https://codeload.github.com/{repo}/zip/{sha}',archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        roots={PurePosixPath(n).parts[0].lower() for n in archive.namelist() if PurePosixPath(n).parts}
        if roots!={repo.split('/')[1]+'-'+sha}:raise ValueError('Archive root does not match repository/base_sha')
        for info in sorted(archive.infolist(),key=lambda i:i.filename):
            parts=PurePosixPath(info.filename).parts
            if len(parts)<2 or info.is_dir() or not info.filename.endswith('.py'):continue
            path='/'.join(parts[1:]);validate_path(path)
            if (info.external_attr>>16)&0o170000==0o120000 or info.file_size>max_bytes:continue
            try:text=archive.read(info).decode('utf-8')
            except UnicodeError:continue
            if '\x00' not in text:yield path,text


def bounded_query(text,tokenizer,config):
    tokens=tokenizer(text,add_special_tokens=False,verbose=False)['input_ids'];count=len(tokens)
    budget=config['evaluation']['max_query_body_tokens']
    if count<=budget and len(tokenizer(config['model']['query_prefix']+text,verbose=False)['input_ids'])<=512:return text,count,False
    while budget>0:
        head=(budget+1)//2;tail=budget//2
        shortened=tokenizer.decode(tokens[:head]+(tokens[-tail:] if tail else []),skip_special_tokens=True)
        if len(tokenizer(config['model']['query_prefix']+shortened,verbose=False)['input_ids'])<=512:return shortened,count,True
        budget-=1
    raise ValueError('Unable to shorten issue query')


def evaluation_rows(rows,snapshots,config,tokenizer,allow_download,limit=None):
    chunks=[];queries=[];failures=[];snapshot_cache={}
    chosen=sorted(rows,key=lambda r:str(r.get('text_id',r.get('id'))))
    if limit is not None:chosen=chosen[:limit]
    for index,row in enumerate(chosen,1):
        repo=repo_name(row);sha=str(row['base_sha']);snapshot_id=digest([repo,sha])
        query_id=str(row.get('text_id') or row.get('id') or digest([repo,sha,row.get('issue_title'),row.get('issue_body')]))
        print(f'Prepare LCA {index}/{len(chosen)}: {repo} @ {sha[:8]}',flush=True)
        try:
            changed=row['changed_files']
            changed=ast.literal_eval(changed) if isinstance(changed,str) else list(changed)
            if not changed or not all(isinstance(p,str) for p in changed):raise ValueError('Invalid ground truth')
            for path in changed:validate_path(path)
            relevant=sorted(set(changed))
            if any(not p.endswith('.py') for p in relevant):raise ValueError('Non-Python ground truth in py evaluation')
            if snapshot_id not in snapshot_cache:
                snapshot_chunks=[]
                for path,text in snapshot_files(snapshots,repo,sha,allow_download,config):
                    if not config['evaluation']['include_test_files'] and any(p in ('test','tests') for p in PurePosixPath(path).parts):continue
                    doc=dict(document_id=digest([repo,sha,path]),text=text)
                    for c in chunk_document(doc,tokenizer,config['model']['revision']):
                        snapshot_chunks.append(dict(c,repo=repo,commit_sha=sha,path=path,snapshot_id=snapshot_id))
                snapshot_cache[snapshot_id]=snapshot_chunks
                chunks.extend(snapshot_chunks)
            paths={c['path'] for c in snapshot_cache[snapshot_id]}
            missing=sorted(set(relevant)-paths)
            if missing:raise ValueError('Missing/unsearchable ground-truth files: '+', '.join(missing))
            text=str(row.get('issue_title','')).strip()+'\n\n'+str(row.get('issue_body') or '').strip()
            if not text.strip():raise ValueError('Empty issue query')
            text,original_tokens,truncated=bounded_query(text.strip(),tokenizer,config)
            queries.append(dict(query_id=query_id,question=text,repo=repo,commit_sha=sha,snapshot_id=snapshot_id,
                relevant_ids=relevant,original_query_tokens=original_tokens,query_truncated=truncated))
        except (ValueError,requests.RequestException,zipfile.BadZipFile) as error:
            failures.append(dict(query_id=query_id,repo=repo,base_sha=sha,error=str(error)[:1500]))
    if not queries:raise ValueError('No usable LCA queries: '+json.dumps(failures[:3],ensure_ascii=False))
    if len({q['query_id'] for q in queries})!=len(queries):raise ValueError('Duplicate evaluation query IDs')
    used={q['snapshot_id'] for q in queries}
    return queries,[c for c in chunks if c['snapshot_id'] in used],failures,len(chosen)


def prepare(config,train_input,valid_input,lca_input,snapshots,output,allow_download=False,limit=None):
    output=output_path(output)
    if output.exists() and any(output.iterdir()):raise FileExistsError('Prepared output already contains data: '+str(output))
    output.mkdir(parents=True,exist_ok=True)
    write_json(output/'config.json',config)
    manifest=dict(status='running',started_at=timestamp(),protocol_hash=protocol_hash(config),
                  source_hashes={},files={},dataset_id=None)
    write_json(output/'manifest.json',manifest)
    try:
        tokenizer=load_tokenizer(config);raw_rows=lca_rows(lca_input)
        excluded={repo_name(r) for r in raw_rows}
        valid,vs,valid_repos,codes,queries=pairs(valid_input,'valid',config,tokenizer,excluded)
        train,ts,_,_,_=pairs(train_input,'train',config,tokenizer,excluded|valid_repos,codes,queries)
        eval_queries,eval_chunks,failures,requested=evaluation_rows(raw_rows,Path(snapshots),config,tokenizer,allow_download,limit)
        for name,rows in [('train.jsonl',train),('valid.jsonl',valid),('eval_queries.jsonl',eval_queries),
                          ('eval_chunks.jsonl',eval_chunks),('excluded_queries.jsonl',failures)]:write_jsonl(output/name,rows)
        sources=sorted(set(source_files(train_input)+source_files(valid_input)+[Path(lca_input)]))
        manifest.update(source_hashes={str(p.resolve()):file_hash(p) for p in sources},
            files={p.name:file_hash(p) for p in output.glob('*.jsonl')},
            counts=dict(train=len(train),valid=len(valid),evaluation_queries=len(eval_queries),evaluation_chunks=len(eval_chunks),
                        requested_queries=requested,excluded_queries=len(failures)),filter_stats=dict(train=ts,valid=vs),
            evaluation_subset=limit is not None,excluded_training_repositories=sorted(excluded),
            dataset_revision=config['evaluation']['revision'],query_truncations=sum(q['query_truncated'] for q in eval_queries))
        manifest['snapshot_content_hashes']={snapshot:digest([(c['path'],c['chunk_id'],c['text']) for c in eval_chunks if c['snapshot_id']==snapshot])
            for snapshot in sorted({c['snapshot_id'] for c in eval_chunks})}
        manifest.update(dataset_id=digest(manifest['files']),status='completed',finished_at=timestamp())
    except BaseException as error:
        manifest.update(status='interrupted' if isinstance(error,KeyboardInterrupt) else 'failed',error=str(error)[:1500],finished_at=timestamp())
        raise
    finally:write_json(output/'manifest.json',manifest)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(HERE/'config.json'))
    parser.add_argument('--download',action='store_true',help='Download missing official dataset archives and base_sha snapshots')
    parser.add_argument('--train-input',type=Path,help='CodeSearchNet train Parquet, JSONL/GZ directory or original ZIP')
    parser.add_argument('--valid-input',type=Path,help='CodeSearchNet valid Parquet, JSONL/GZ directory or original ZIP')
    parser.add_argument('--lca-input',type=Path,help='LCA py/test Parquet or JSONL')
    parser.add_argument('--snapshots',type=Path,help='owner__repo/base_sha/ source trees or owner__repo/base_sha.zip')
    parser.add_argument('--output',help='New prepared directory inside data/experiments')
    parser.add_argument('--limit-eval-queries',type=int,help='Explicit subset for a small test; recorded in manifest')
    args=parser.parse_args();config=load_config(args.config);raw=output_path(config['paths']['raw'])
    if args.limit_eval_queries is not None and args.limit_eval_queries<1:parser.error('limit must be positive')
    revision=config['data'].get('hub_revision',CODESEARCHNET_REVISION)
    source_repo=config['data'].get('hub_dataset',CODESEARCHNET_REPO)
    source_root=raw/'codesearchnet'/revision
    train_default=source_root/'python-train.parquet'
    valid_default=source_root/'python-valid.parquet'
    lca=raw/'lca'/config['evaluation']['revision']/'py-test.parquet'
    train=args.train_input or train_default;valid=args.valid_input or valid_default;lca=args.lca_input or lca
    if args.download:
        source_url=f'https://huggingface.co/datasets/{source_repo}/resolve/{revision}/python'
        if not args.train_input:download(source_url+'/train-00000-of-00001.parquet',train)
        if not args.valid_input:download(source_url+'/validation-00000-of-00001.parquet',valid)
        if not args.lca_input:
            revision=config['evaluation']['revision']
            download(f'https://huggingface.co/datasets/JetBrains-Research/lca-bug-localization/resolve/{revision}/py/test-00000-of-00001.parquet',lca)
    for path in (train,valid,lca):
        if not path.exists():parser.error(str(path)+' is missing. Supply inputs or use --download.')
    snapshots=args.snapshots or raw/'snapshots'
    if args.download:snapshots=output_path(snapshots)
    manifest=prepare(config,train,valid,lca,snapshots,args.output or config['paths']['prepared'],args.download,args.limit_eval_queries)
    print(json.dumps(manifest['counts'],ensure_ascii=False,indent=2))


if __name__=='__main__':main()
