"""같은 평가 청크를 모델별로 재인코딩하고 파일 검색 Recall/MRR/nDCG를 측정한다."""
import argparse
from pathlib import Path
import torch
from transformers import AutoModel
from common import (HERE, Run, checked_checkpoint, load_config, load_tokenizer, model_source,
                    prepared_data, read_json, read_jsonl, write_json, write_jsonl)
from retrieval import encode, file_ranking, summarize, validation
from app.ml.encoder import select_device


def evaluate(config,prepared,run_name,model_path=None,dataset='lca',device_requested=None):
    folder,data=prepared_data(config,prepared)
    device=select_device(device_requested or config['training']['device'],torch)
    identity=dict(model_id=config['model']['model_id'],revision=config['model']['revision'])
    if model_path:
        model_path=Path(model_path).resolve()
        if (model_path/'best.json').is_file():model_path=Path(read_json(model_path/'best.json')['checkpoint'])
        source,metadata=checked_checkpoint(model_path,config)
        identity=dict(checkpoint=str(source),**metadata)
    else:
        source=model_source(config)
    with Run(config,run_name,'evaluate_'+dataset,data['dataset_id']) as run:
        run.manifest.update(device=device,compute_dtype='float32',model=identity,
                            evaluation_subset=data.get('evaluation_subset',False))
        tokenizer=load_tokenizer(config,source)
        model=AutoModel.from_pretrained(source,local_files_only=True,trust_remote_code=False,token=False,dtype=torch.float32).to(device).eval()
        if model.config.hidden_size!=384:raise ValueError('E5 dimension mismatch')
        if dataset=='valid':
            val_loss,metrics,rankings=validation(model,tokenizer,read_jsonl(folder/'valid.jsonl'),config,device)
            result=dict(split='valid',evaluation_unit='function',metrics=metrics,validation_loss=val_loss)
        else:
            queries=read_jsonl(folder/'eval_queries.jsonl');chunks=read_jsonl(folder/'eval_chunks.jsonl')
            m=config['model'];batch=config['training']['batch_size'];rankings=[]
            ks=config['evaluation']['metrics'];top=max(k for values in ks.values() for k in values)
            snapshots=sorted({q['snapshot_id'] for q in queries})
            for index,snapshot in enumerate(snapshots,1):
                candidates=[c for c in chunks if c['snapshot_id']==snapshot]
                qs=[q for q in queries if q['snapshot_id']==snapshot]
                print(f'Evaluate snapshot {index}/{len(snapshots)}: {qs[0]["repo"]}, {len(candidates)} chunks',flush=True)
                corpus=encode(model,tokenizer,[c['text'] for c in candidates],m['passage_prefix'],device,batch)
                q_vectors=encode(model,tokenizer,[q['question'] for q in qs],m['query_prefix'],device,batch)
                for q,vector in zip(qs,q_vectors):
                    candidate_paths={c['path'] for c in candidates}
                    if not set(q['relevant_ids']).issubset(candidate_paths):raise ValueError('Ground truth not in candidate corpus')
                    ranked=file_ranking(candidates,corpus@vector,top)
                    for rank,r in enumerate(ranked,1):r.update(rank=rank,relevant=r['item_id'] in q['relevant_ids'])
                    rankings.append(dict(q,query_vectors_model=identity,corpus_vectors_model=identity,retrieved=ranked))
            metrics=summarize(rankings,ks)
            result=dict(split='test',evaluation_unit='file',metrics=metrics,evaluation_subset=data.get('evaluation_subset',False),
                        requested_queries=data['counts']['requested_queries'],excluded_queries=data['counts']['excluded_queries'])
        result.update(dataset_id=data['dataset_id'],model=identity,query_count=len(rankings))
        write_json(run.folder/'metrics.json',result);write_jsonl(run.folder/'rankings.jsonl',rankings)
        run.manifest.update(metrics=metrics,query_count=len(rankings))
        print('Metrics:',metrics,flush=True)
    return run.folder


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default=str(HERE/'config.json'))
    parser.add_argument('--prepared')
    parser.add_argument('--run-name',required=True)
    parser.add_argument('--model',type=Path,help='E001 checkpoint or training run folder; omitted means original pinned E5')
    parser.add_argument('--dataset',choices=['lca','valid'],default='lca')
    parser.add_argument('--device',choices=['auto','cpu','cuda'])
    args=parser.parse_args();config=load_config(args.config)
    if args.device:config['training']['device']=args.device
    evaluate(config,args.prepared,args.run_name,args.model,args.dataset,args.device)


if __name__=='__main__':main()
