"""E5 입력/풀링과 검색 지표. 학습·baseline·학습 모델 평가가 함께 사용한다."""
import math
import numpy as np


def embeddings(model, inputs):
    import torch
    hidden=model(**inputs).last_hidden_state
    mask=inputs['attention_mask'].unsqueeze(-1).to(hidden.dtype)
    pooled=(hidden*mask).sum(1)/mask.sum(1).clamp_min(1)
    return torch.nn.functional.normalize(pooled.float(),p=2,dim=1)


def tokenize(tokenizer,texts,prefix,device):
    batch=tokenizer([prefix+t for t in texts],padding=True,truncation=False,return_tensors='pt')
    if batch['input_ids'].shape[-1]>512:raise ValueError('E5 전체 입력이 512 token을 초과합니다.')
    return {k:v.to(device) for k,v in batch.items()}


def encode(model,tokenizer,texts,prefix,device,batch_size=16):
    import torch
    model.eval();rows=[]
    with torch.inference_mode():
        for start in range(0,len(texts),batch_size):
            vectors=embeddings(model,tokenize(tokenizer,texts[start:start+batch_size],prefix,device))
            rows.append(vectors.cpu().numpy())
    result=np.concatenate(rows).astype(np.float32) if rows else np.empty((0,384),dtype=np.float32)
    if result.shape!=(len(texts),384) or not np.isfinite(result).all():raise ValueError('잘못된 모델 출력')
    if len(result) and not np.allclose(np.linalg.norm(result,axis=1),1,atol=1e-4):raise ValueError('벡터 정규화 오류')
    return result


def query_metrics(retrieved,relevant,ks):
    relevant=set(relevant)
    if not relevant:raise ValueError('정답이 없는 질문은 평가할 수 없습니다.')
    if len(set(retrieved))!=len(retrieved):raise ValueError('검색 순위에 중복 item이 있습니다.')
    values={}
    for k in ks['recall_at_k']:
        values[f'recall_at_{k}']=len(set(retrieved[:k])&relevant)/len(relevant)
    for k in ks['mrr_at_k']:
        values[f'mrr_at_{k}']=next((1/i for i,item in enumerate(retrieved[:k],1) if item in relevant),0.)
    for k in ks['ndcg_at_k']:
        dcg=sum(1/math.log2(i+1) for i,item in enumerate(retrieved[:k],1) if item in relevant)
        ideal=sum(1/math.log2(i+1) for i in range(1,min(k,len(relevant))+1))
        values[f'ndcg_at_{k}']=dcg/ideal
    return values


def summarize(rankings,ks):
    if not rankings:raise ValueError('평가 가능한 질문이 없습니다.')
    scores=[query_metrics([r['item_id'] for r in row['retrieved']],row['relevant_ids'],ks) for row in rankings]
    return {name:float(np.mean([s[name] for s in scores])) for name in scores[0]}


def file_ranking(chunks,scores,top_k):
    # 전체 청크 점수에서 파일별 최대를 구한 뒤 파일 Top K를 선택한다.
    best={}
    for c,score in zip(chunks,scores):
        old=best.get(c['path'])
        if old is None or score>old['score'] or (score==old['score'] and c['chunk_id']<old['chunk_id']):
            best[c['path']]=dict(item_id=c['path'],path=c['path'],score=float(score),
                chunk_id=c['chunk_id'],start_line=c['start_line'],end_line=c['end_line'],text=c['text'])
    return sorted(best.values(),key=lambda r:(-r['score'],r['path']))[:top_k]


def validation(model,tokenizer,rows,config,device):
    m=config['model'];batch=config['training']['batch_size'];ks={'recall_at_k':[5],'mrr_at_k':[10],'ndcg_at_k':[10]}
    corpus=encode(model,tokenizer,[r['positive'] for r in rows],m['passage_prefix'],device,batch)
    queries=encode(model,tokenizer,[r['query'] for r in rows],m['query_prefix'],device,batch)
    rankings=[]
    for row,vector in zip(rows,queries):
        scores=corpus@vector
        order=sorted(range(len(rows)),key=lambda i:(-float(scores[i]),rows[i]['pair_id']))[:10]
        rankings.append(dict(query_id=row['pair_id'],question=row['query'],relevant_ids=[row['pair_id']],
            retrieved=[dict(item_id=rows[i]['pair_id'],path=rows[i]['path'],rank=rank,score=float(scores[i]),
                            relevant=rows[i]['pair_id']==row['pair_id']) for rank,i in enumerate(order,1)]))
    # Validation loss에서도 전체 valid 후보를 negatives로 사용한다.
    logits=(queries@corpus.T).astype(np.float64)/config['training']['temperature']
    peaks=logits.max(axis=1)
    loss=float(np.mean(peaks+np.log(np.exp(logits-peaks[:,None]).sum(axis=1))-np.diag(logits)))
    return loss,summarize(rankings,ks),rankings
