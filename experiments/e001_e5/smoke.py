"""작은 수작업 데이터와 실제 E5로 준비·평가·학습·재개를 검증한다. 검색 품질 벤치마크는 아니다."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from common import HERE, ROOT, file_hash, load_config, output_path, read_json, write_json, write_jsonl


def fixtures(root,config):
    raw=root/'raw';raw.mkdir(parents=True)
    examples=[('Add two integers','add','return a + b'),('Subtract two integers','subtract','return a - b'),
              ('Multiply two integers','multiply','return a * b'),('Divide two numbers','divide','return a / b'),
              ('Find the greater number','maximum','return a if a > b else b'),('Compare two numbers for equality','equal','return a == b')]
    valid=[('Calculate the absolute value','absolute','return -a if a < 0 else a'),
           ('Calculate the square of a number','square','return a * a'),('Check whether a number is even','even','return a % 2 == 0')]
    def rows(items,repo,split):
        return [dict(repo=repo,path=name+'.py',partition=split,language='python',docstring=q,
                     code=f'def {name}(a, b=0):\n    """{q}"""\n    {body}\n') for q,name,body in items]
    write_jsonl(raw/'train.jsonl',rows(examples,'fixture/train','train'))
    write_jsonl(raw/'valid.jsonl',rows(valid,'fixture/valid','valid'))
    sha='a'*40;snapshot=raw/'snapshots/fixture__smoke'/sha;snapshot.mkdir(parents=True)
    (snapshot/'arithmetic.py').write_text('def add(a, b):\n    return a + b\n\ndef subtract(a, b):\n    return a - b\n',encoding='utf-8')
    (snapshot/'files.py').write_text('from pathlib import Path\n\ndef exists(path):\n    return Path(path).exists()\n',encoding='utf-8')
    (snapshot/'greeting.py').write_text('def greet(name):\n    return "Hello " + name\n',encoding='utf-8')
    write_jsonl(raw/'lca.jsonl',[
        dict(text_id='smoke-add',repo_owner='fixture',repo_name='smoke',base_sha=sha,changed_files=['arithmetic.py'],
             issue_title='Addition gives the wrong result',issue_body='Find the function that adds two numbers.'),
        dict(text_id='smoke-files',repo_owner='fixture',repo_name='smoke',base_sha=sha,changed_files=['files.py'],
             issue_title='File existence check returns an incorrect result',issue_body='Locate the path existence check.')])
    config=copy.deepcopy(config)
    config['data'].update(train_pairs=6,validation_pairs=3)
    config['training'].update(device='cpu',batch_size=2,epochs=1,max_steps=2,validation_every_steps=1,logging_every_steps=1)
    config['paths']={k:str(root/k) for k in ('raw','prepared','runs')}
    write_json(root/'config.json',config)
    return config


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keep-models',action='store_true',help='Retain several GB of smoke checkpoint weights/optimizer states')
    args=parser.parse_args()
    root=output_path('data/experiments/e001-smoke/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    root.mkdir(parents=True,exist_ok=False);config=fixtures(root,load_config())
    env=os.environ.copy();env.update(OMP_NUM_THREADS='4',MKL_NUM_THREADS='4',TOKENIZERS_PARALLELISM='false')
    commands=[]
    def execute(file,*extra):
        command=[sys.executable,str(HERE/file),'--config',str(root/'config.json'),*extra]
        commands.append(command);print('\nSMOKE:',file,' '.join(extra),flush=True)
        subprocess.run(command,cwd=ROOT,env=env,check=True)
    execute('prepare.py','--train-input',str(root/'raw/train.jsonl'),'--valid-input',str(root/'raw/valid.jsonl'),
            '--lca-input',str(root/'raw/lca.jsonl'),'--snapshots',str(root/'raw/snapshots'))
    execute('evaluate.py','--run-name','baseline')
    execute('train.py','--run-name','train')
    step1=root/'runs/train/checkpoints/step_000001';step2=root/'runs/train/checkpoints/step_000002'
    if file_hash(step1/'model.safetensors')==file_hash(step2/'model.safetensors'):
        raise AssertionError('Actual model weights did not change during training')
    execute('train.py','--run-name','resumed','--resume',str(step1))
    resumed=root/'runs/resumed/checkpoints/step_000002'
    if file_hash(step2/'model.safetensors')!=file_hash(resumed/'model.safetensors'):
        raise AssertionError('Resumed CPU training differs from uninterrupted training')
    execute('evaluate.py','--run-name','tuned','--model',str(root/'runs/train'))
    baseline=read_json(root/'runs/baseline/metrics.json');tuned=read_json(root/'runs/tuned/metrics.json')
    if baseline['dataset_id']!=tuned['dataset_id']:raise AssertionError('Evaluation corpora differ')
    write_json(root/'smoke-result.json',dict(status='passed',fixture=True,device='cpu',actual_e5_weights_changed=True,
        resumed_weights_match=True,models_retained=args.keep_models,baseline=baseline['metrics'],tuned=tuned['metrics'],commands=commands))
    if not args.keep_models:
        # 이번 smoke가 만든 큰 텐서 파일만 지우고 설정과 결과는 남긴다.
        for pattern in ('model.safetensors','training_state.pt'):
            for path in root.rglob(pattern):
                if not path.resolve().is_relative_to(root.resolve()):raise ValueError('Smoke cleanup path escaped')
                path.unlink()
    print('\nPASS: real E5 prepare/baseline/gradient updates/resume/tuned evaluation; artifacts:',root,flush=True)
    if not args.keep_models:print('Large smoke checkpoint tensors removed; logs/results retained.')


if __name__=='__main__':main()
