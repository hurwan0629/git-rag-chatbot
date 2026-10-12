"""실험 설정, 데이터 무결성, 실행 기록. 서비스 DB에는 접근하지 않는다."""
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[2]
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'backend'))
from app.ml.encoder import cached_source


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    temporary.replace(path)


def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as stream:
        return [json.loads(line) for line in stream if line.strip()]


def write_jsonl(path, rows):
    with Path(path).open('w',encoding='utf-8') as stream:
        for row in rows:stream.write(json.dumps(row,ensure_ascii=False,allow_nan=False)+'\n')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def output_path(value):
    path=Path(value)
    path=(path if path.is_absolute() else ROOT/path).resolve()
    if not path.is_relative_to((ROOT/'data/experiments').resolve()):
        raise ValueError('실험 출력은 프로젝트의 data/experiments/ 아래에 지정하세요.')
    return path


def load_config(path=None):
    config=read_json(path or HERE/'config.json')
    m=config['model'];t=config['training'];e=config['evaluation'];d=config['data']
    if (m['model_id']!='intfloat/multilingual-e5-small' or m['dimension']!=384
        or m['max_input_tokens']!=512 or m['pooling']!='masked_mean' or not m['normalize']
        or m['query_prefix']!='query: ' or m['passage_prefix']!='passage: '
        or not re.fullmatch('[a-f0-9]{40}',m['revision'])):raise ValueError('E001 E5 설정 불일치')
    for key in ('train_pairs','validation_pairs'):
        if type(d[key]) is not int or d[key]<2:raise ValueError(key+'는 2 이상이어야 합니다.')
    for key in ('batch_size','epochs','validation_every_steps','logging_every_steps'):
        if type(t[key]) is not int or t[key]<1:raise ValueError('Invalid '+key)
    if t.get('max_steps') is not None and (type(t['max_steps']) is not int or t['max_steps']<1):raise ValueError('Invalid max_steps')
    for key in ('temperature','learning_rate','max_grad_norm'):
        if not isinstance(t[key],(int,float)) or not math.isfinite(t[key]) or t[key]<=0:raise ValueError('Invalid '+key)
    if not 0<=t['warmup_ratio']<1 or not math.isfinite(t['weight_decay']) or t['weight_decay']<0:raise ValueError('Invalid optimizer settings')
    if type(config['seed']) is not int or not 0<=config['seed']<2**32:raise ValueError('Invalid seed')
    if t['device'] not in ('auto','cpu','cuda'):raise ValueError('Invalid device')
    if t['precision']!='bf16_if_supported_else_fp32':raise ValueError('지원하지 않는 precision')
    if (t['loss']!='infonce' or t['negative_mode']!='in_batch' or t['optimizer']!='AdamW'
        or t['checkpoint_metric']!='validation_mrr_at_10' or t['checkpoint_metric_mode']!='max'
        or t['batch_size']<2 or t['epochs']<1 or t['temperature']<=0 or t['learning_rate']<=0
        or t['validation_every_steps']<1):raise ValueError('지원하지 않는 학습 설정')
    if (d['language']!='python' or d['train_split']!='train' or d['validation_split']!='valid'
        or d['positive_docstring_policy']!='remove_query_docstring'
        or d['code_overflow_policy']!='exclude_pair' or d['query_overflow_policy']!='exclude_pair'
        or not all(d[k] for k in ('preserve_repository_splits','exclude_evaluation_repositories','deduplicate_pairs'))):
        raise ValueError('지원하지 않는 데이터 전처리 설정')
    if (d.get('hub_dataset','code-search-net/code_search_net')!='code-search-net/code_search_net'
        or not re.fullmatch('[a-f0-9]{40}',d.get('hub_revision','bd0cf261e357a3eb5c8fba490d23ec1a1cd59555'))):
        raise ValueError('Invalid CodeSearchNet source/revision')
    if (e['configuration']!='py' or e['split']!='test' or e['snapshot']!='base_sha'
        or e['file_score']!='max_chunk_score' or e['unit']!='file' or e['similarity']!='cosine'
        or e['query_overflow_policy']!='head_tail_tokens' or e['candidate_extensions']!=['.py']
        or e['query_fields']!=['issue_title','issue_body']):raise ValueError('지원하지 않는 평가 설정')
    if (e['dataset']!='JetBrains-Research/lca-bug-localization' or not re.fullmatch('[a-f0-9]{40}',e['revision'])
        or type(e['max_query_body_tokens']) is not int or not 1<=e['max_query_body_tokens']<=500
        or type(e['max_file_bytes']) is not int or e['max_file_bytes']<1):raise ValueError('Invalid evaluation limits/revision')
    from app.services.config import CHUNKING
    if e['chunking']!=CHUNKING:raise ValueError('서비스와 동일한 청킹 설정이 필요합니다.')
    if set(e['metrics'])!={'recall_at_k','mrr_at_k','ndcg_at_k'}:raise ValueError('Invalid metric names')
    for values in e['metrics'].values():
        if not values or any(type(k) is not int or k<1 for k in values):raise ValueError('잘못된 평가 K')
    for path in config['paths'].values():output_path(path)
    return config


def protocol_hash(config):
    return digest({key:config[key] for key in ('model','data','evaluation')})


def model_source(config):
    m=config['model']
    return cached_source(m['model_id'],m['revision'],ROOT/'data/model-cache')


def load_tokenizer(config, source=None):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(source or model_source(config),local_files_only=True,trust_remote_code=False,token=False)


def prepared_data(config, folder=None):
    folder=output_path(folder or config['paths']['prepared'])
    manifest=read_json(folder/'manifest.json')
    if manifest['status']!='completed' or manifest['protocol_hash']!=protocol_hash(config):
        raise ValueError('prepared 데이터가 없거나 config와 조건이 다릅니다. prepare를 다시 실행하세요.')
    for name,expected in manifest['files'].items():
        if Path(name).name!=name or not name.endswith('.jsonl'):raise ValueError('Invalid prepared filename')
        if file_hash(folder/name)!=expected:raise ValueError('prepared 파일 변조: '+name)
    if manifest['dataset_id']!=digest(manifest['files']):raise ValueError('prepared ID 불일치')
    return folder,manifest


def timestamp():return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, config, name, kind, dataset_id):
        if not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,79}',name):raise ValueError('안전한 run 이름이 필요합니다.')
        self.folder=output_path(config['paths']['runs'])/name
        self.folder.mkdir(parents=True,exist_ok=False)
        write_json(self.folder/'config.json',config)
        try:
            commit=subprocess.run(['git','rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True,timeout=5).stdout.strip()
        except (OSError,subprocess.TimeoutExpired):commit=None
        self.manifest=dict(run_id=name,kind=kind,status='running',started_at=timestamp(),finished_at=None,
            dataset_id=dataset_id,config_hash=digest(config),git_commit=commit,
            code_hashes={p.name:file_hash(p) for p in HERE.glob('*.py')},
            versions={k:importlib.metadata.version(k) for k in ('torch','transformers','numpy','requests','pyarrow')})
        write_json(self.folder/'manifest.json',self.manifest)

    def __enter__(self):return self

    def __exit__(self, kind, error, traceback):
        self.manifest.update(status='completed' if error is None else 'interrupted' if kind==KeyboardInterrupt else 'failed',finished_at=timestamp())
        if error:
            message=str(error)
            for name in ('GITHUG_TOKEN','GITHUB_TOKEN','HF_TOKEN','POSTGRES_PASSWORD'):
                if os.getenv(name):message=message.replace(os.environ[name],'[REDACTED]')
            self.manifest['error']=message[:1500]
        write_json(self.folder/'manifest.json',self.manifest)


def checkpoint_identity(source):
    source=Path(source)
    files={p.name:file_hash(p) for p in source.iterdir() if p.is_file() and
           (p.suffix=='.safetensors' or p.name in ('config.json','tokenizer.json','tokenizer_config.json','sentencepiece.bpe.model'))}
    if not any(name.endswith('.safetensors') for name in files):raise ValueError('체크포인트 가중치가 없습니다.')
    return dict(model_hash=digest(files),files=files)


def checked_checkpoint(path, config):
    path=Path(path).resolve();metadata=read_json(path/'e001_model.json')
    if metadata['base_model']!=config['model']:raise ValueError('체크포인트의 E5 입력 설정이 다릅니다.')
    if checkpoint_identity(path)!=metadata['identity']:raise ValueError('체크포인트 파일 hash 불일치')
    return path,metadata
