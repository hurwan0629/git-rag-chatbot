"""E5 전체 가중치를 InfoNCE로 학습하고 valid MRR@10으로 체크포인트를 선정한다."""
import argparse
import csv
import math
from pathlib import Path
import random
import numpy as np
import torch
from transformers import AutoModel
from common import (HERE, Run, checked_checkpoint, checkpoint_identity, digest, load_config,
                    load_tokenizer, model_source, prepared_data, read_jsonl, write_json)
from retrieval import embeddings, tokenize, validation
from app.ml.encoder import select_device


def batches(rows,size,seed,epoch):
    order=list(range(len(rows)));random.Random(seed+epoch).shuffle(order)
    return [[rows[i] for i in order[start:start+size]] for start in range(0,len(rows),size)
            if len(order[start:start+size])>1]


def save_checkpoint(run,model,tokenizer,config,state):
    folder=run.folder/'checkpoints'/f"step_{state['global_step']:06d}"
    folder.mkdir(parents=True,exist_ok=False)
    model.save_pretrained(folder,safe_serialization=True);tokenizer.save_pretrained(folder)
    write_json(folder/'e001_model.json',dict(base_model=config['model'],identity=checkpoint_identity(folder),
        dataset_id=state['dataset_id'],training_hash=state['training_hash']))
    torch.save(state,folder/'training_state.pt')
    write_json(run.folder/'latest.json',dict(checkpoint=str(folder.resolve())))
    return folder


def train(config,prepared,run_name,resume=None):
    folder,data=prepared_data(config,prepared);rows=read_jsonl(folder/'train.jsonl');valid=read_jsonl(folder/'valid.jsonl')
    if len(rows)<2 or len(valid)<2:raise ValueError('학습·검증 쌍이 각각 2개 이상 필요합니다.')
    if set(r['repo'] for r in rows)&set(r['repo'] for r in valid):raise ValueError('학습/검증 저장소 중복')
    t=config['training'];m=config['model'];device=select_device(t['device'],torch)
    torch.manual_seed(config['seed']);np.random.seed(config['seed'])
    if device=='cuda':torch.cuda.manual_seed_all(config['seed'])
    bf16=device=='cuda' and torch.cuda.is_bf16_supported()
    if t['precision']!='bf16_if_supported_else_fp32':raise ValueError('지원하지 않는 학습 precision')
    training_hash=digest(dict(model=m,training=t,dataset_id=data['dataset_id']))
    state=None
    if resume:
        source,metadata=checked_checkpoint(resume,config)
        if metadata['training_hash']!=training_hash:raise ValueError('재개할 데이터/학습 설정이 다릅니다.')
        state=torch.load(source/'training_state.pt',map_location='cpu',weights_only=True)
    else:
        source=model_source(config)
    tokenizer=load_tokenizer(config,source)
    with Run(config,run_name,'train',data['dataset_id']) as run:
        run.manifest.update(device=device,compute_dtype='bfloat16' if bf16 else 'float32',
            precision=t['precision'],resume_from=str(source) if resume else None)
        print('Loading training model on',device,flush=True)
        model=AutoModel.from_pretrained(source,local_files_only=True,trust_remote_code=False,token=False,dtype=torch.float32).to(device)
        if model.config.hidden_size!=384:raise ValueError('E5 dimension mismatch')
        model.gradient_checkpointing_enable()
        optimizer=torch.optim.AdamW(model.parameters(),lr=t['learning_rate'],weight_decay=t['weight_decay'])
        steps_per_epoch=len(batches(rows,t['batch_size'],config['seed'],0))
        total_steps=steps_per_epoch*t['epochs']
        if t.get('max_steps') is not None:total_steps=min(total_steps,t['max_steps'])
        warmup=math.floor(total_steps*t['warmup_ratio'])
        def lr_scale(step):
            if warmup and step<warmup:return (step+1)/warmup
            return max(0.,(total_steps-step)/max(1,total_steps-warmup))
        scheduler=torch.optim.lr_scheduler.LambdaLR(optimizer,lr_scale)
        global_step=0;start_epoch=0;start_batch=0;best=-1.;best_checkpoint=None
        if state:
            if state['total_steps']!=total_steps or state['training_hash']!=training_hash:raise ValueError('학습 재개 스케줄 불일치')
            optimizer.load_state_dict(state['optimizer']);scheduler.load_state_dict(state['scheduler'])
            global_step=state['global_step'];start_epoch=state['epoch'];start_batch=state['next_batch']
            best=state['best_metric'];best_checkpoint=state['best_checkpoint']
            torch.set_rng_state(state['rng_state'])
            if device=='cuda' and state['cuda_rng_state']:torch.cuda.set_rng_state_all(state['cuda_rng_state'])
            if global_step>=total_steps:raise ValueError('이미 목표 step까지 완료된 체크포인트입니다.')
        run.manifest.update(total_steps=total_steps,training_hash=training_hash,
            dropped_singleton_pairs_per_epoch=len(rows)%t['batch_size']==1)
        fields=['step','epoch','train_loss','val_loss','validation_mrr_at_10','learning_rate','validation_recall_at_5','validation_ndcg_at_10']
        with (run.folder/'history.csv').open('w',encoding='utf-8',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
            done=False
            for epoch in range(start_epoch,t['epochs']):
                epoch_batches=batches(rows,t['batch_size'],config['seed'],epoch)
                for position,batch in enumerate(epoch_batches):
                    if epoch==start_epoch and position<start_batch:continue
                    model.train();optimizer.zero_grad(set_to_none=True)
                    with torch.autocast(device_type=device,dtype=torch.bfloat16,enabled=bf16):
                        # 질문 i와 코드 i가 정답 쌍이고, 같은 배치의 다른 코드는 negative이다.
                        query=embeddings(model,tokenize(tokenizer,[r['query'] for r in batch],m['query_prefix'],device))
                        positive=embeddings(model,tokenize(tokenizer,[r['positive'] for r in batch],m['passage_prefix'],device))
                        logits=(query@positive.T)/t['temperature']
                        loss=torch.nn.functional.cross_entropy(logits.float(),torch.arange(len(batch),device=device))
                    if not torch.isfinite(loss):raise ValueError('학습 loss가 유한값이 아닙니다.')
                    loss.backward();torch.nn.utils.clip_grad_norm_(model.parameters(),t['max_grad_norm'])
                    learning_rate=optimizer.param_groups[0]['lr'];optimizer.step();scheduler.step();global_step+=1
                    record=dict(step=global_step,epoch=epoch+1,train_loss=float(loss.detach()),learning_rate=learning_rate)
                    if global_step%t['validation_every_steps']==0 or global_step==total_steps:
                        val_loss,metrics,_=validation(model,tokenizer,valid,config,device)
                        record.update(val_loss=val_loss,validation_mrr_at_10=metrics['mrr_at_10'],
                                      validation_recall_at_5=metrics['recall_at_5'],validation_ndcg_at_10=metrics['ndcg_at_10'])
                        improved=metrics['mrr_at_10']>best
                        if improved:
                            best=metrics['mrr_at_10']
                            best_checkpoint=str((run.folder/'checkpoints'/f'step_{global_step:06d}').resolve())
                        state=dict(global_step=global_step,epoch=epoch,next_batch=position+1,total_steps=total_steps,
                            dataset_id=data['dataset_id'],training_hash=training_hash,best_metric=best,
                            best_checkpoint=best_checkpoint,optimizer=optimizer.state_dict(),scheduler=scheduler.state_dict(),
                            rng_state=torch.get_rng_state(),cuda_rng_state=torch.cuda.get_rng_state_all() if device=='cuda' else [])
                        checkpoint=save_checkpoint(run,model,tokenizer,config,state)
                        if improved:write_json(run.folder/'best.json',dict(checkpoint=str(checkpoint.resolve()),metric='validation_mrr_at_10',value=best))
                        print(f'Step {global_step}/{total_steps}: loss={record["train_loss"]:.4f}, valid MRR@10={metrics["mrr_at_10"]:.4f}',flush=True)
                    elif global_step%t['logging_every_steps']==0:
                        print(f'Step {global_step}/{total_steps}: loss={record["train_loss"]:.4f}',flush=True)
                    writer.writerow(record);stream.flush()
                    if global_step>=total_steps:done=True;break
                if done:break
        if not best_checkpoint:raise ValueError('선정된 체크포인트가 없습니다.')
        write_json(run.folder/'best.json',dict(checkpoint=best_checkpoint,metric='validation_mrr_at_10',value=best))
        run.manifest.update(global_step=global_step,best_checkpoint=best_checkpoint,best_validation_mrr_at_10=best)
        print('Best checkpoint:',best_checkpoint,flush=True)
    return run.folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(HERE/'config.json'))
    parser.add_argument('--prepared',help='Prepared directory; defaults to config paths.prepared')
    parser.add_argument('--run-name',required=True,help='New run name; existing folders are never overwritten')
    parser.add_argument('--device',choices=['auto','cpu','cuda'])
    parser.add_argument('--max-steps',type=int,help='Short run cap; recorded in the run config')
    parser.add_argument('--resume',type=Path,help='Trusted local E001 checkpoint, continuing optimizer/scheduler/RNG in a NEW run')
    args=parser.parse_args();config=load_config(args.config)
    if args.device:config['training']['device']=args.device
    if args.max_steps is not None:
        if args.max_steps<1:parser.error('max-steps must be positive')
        config['training']['max_steps']=args.max_steps
    train(config,args.prepared,args.run_name,args.resume)


if __name__=='__main__':main()
