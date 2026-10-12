"""데이터 준비 → 원본 평가 → 학습 → 선정 모델 평가를 별도 Python 프로세스로 실행한다."""
import argparse
from datetime import datetime,timezone
from pathlib import Path
import subprocess
import sys
from common import HERE, ROOT, Run, load_config, prepared_data


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(HERE/'config.json'))
    parser.add_argument('--prefix',default=datetime.now(timezone.utc).strftime('e001_%Y%m%dT%H%M%SZ'))
    parser.add_argument('--download',action='store_true')
    parser.add_argument('--skip-prepare',action='store_true',help='Use an existing completed prepared dataset')
    parser.add_argument('--device',choices=['auto','cpu','cuda'])
    parser.add_argument('--max-steps',type=int)
    args=parser.parse_args()
    args.config=str(Path(args.config).resolve())
    config=load_config(args.config)
    if args.max_steps is not None and args.max_steps<1:parser.error('max-steps must be positive')
    if args.device:config['training']['device']=args.device
    if args.max_steps is not None:config['training']['max_steps']=args.max_steps
    def execute(file,extra):
        command=[sys.executable,str(HERE/file),'--config',str(args.config),*extra]
        print('\nRUN:',file,' '.join(extra),flush=True)
        subprocess.run(command,cwd=ROOT,check=True)
    with Run(config,args.prefix+'_pipeline','pipeline',None) as run:
        run.manifest.update(max_steps=config['training']['max_steps'],requested_device=config['training']['device'],
            runs=[args.prefix+'_baseline',args.prefix+'_train',args.prefix+'_tuned'])
        if not args.skip_prepare:execute('prepare.py',['--download'] if args.download else [])
        _,data=prepared_data(config);run.manifest['dataset_id']=data['dataset_id']
        device=['--device',args.device] if args.device else []
        execute('evaluate.py',['--run-name',args.prefix+'_baseline',*device])
        train=['--run-name',args.prefix+'_train',*device]
        if args.max_steps is not None:train+=['--max-steps',str(args.max_steps)]
        execute('train.py',train)
        execute('evaluate.py',['--run-name',args.prefix+'_tuned','--model',str(run.folder.parent/(args.prefix+'_train')),*device])


if __name__=='__main__':main()
