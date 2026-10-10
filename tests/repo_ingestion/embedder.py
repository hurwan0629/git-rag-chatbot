"""같은 revision의 tokenizer/encoder를 한 번 로딩하고 E5 벡터를 만든다."""

from .config import CACHE_DIR, DIMENSION, MAX_INPUT_TOKENS, MODEL_ID, PREFIX, safe_error


def resolve_revision():
    from huggingface_hub import HfApi
    revision = HfApi(token=False).model_info(MODEL_ID).sha
    if not revision:
        raise ValueError('모델 revision SHA를 확인하지 못했습니다.')
    return revision


def load_tokenizer(revision):
    from transformers import AutoTokenizer
    return AutoTokenizer.from_pretrained(MODEL_ID, revision=revision, cache_dir=str(CACHE_DIR),
                                         trust_remote_code=False, token=False)


def load_encoder(revision, requested_device):
    import torch
    from transformers import AutoModel
    if requested_device == 'cuda' and not torch.cuda.is_available():
        raise ValueError('CUDA를 사용할 수 없습니다. --device cpu 또는 auto를 사용하세요.')
    device = ('cuda' if torch.cuda.is_available() else 'cpu') if requested_device == 'auto' else requested_device
    model = AutoModel.from_pretrained(MODEL_ID, revision=revision, cache_dir=str(CACHE_DIR),
                                     trust_remote_code=False, token=False, dtype=torch.float32)
    if model.config.hidden_size != DIMENSION:
        raise ValueError('선정한 모델의 출력 차원이 설계와 다릅니다.')
    return model.to(device).eval(), device


def masked_mean_pool(hidden, attention_mask):
    mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1)


def embed_chunks(chunks, tokenizer, model, device, batch_size):
    import numpy as np
    import torch
    vectors, errors = [], []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start:start + batch_size]
        try:
            inputs = tokenizer([PREFIX + chunk['text'] for chunk in batch], padding=True,
                               truncation=False, return_tensors='pt', verbose=False)
            lengths = inputs['attention_mask'].sum(dim=1).tolist()
            if inputs['input_ids'].shape[1] > MAX_INPUT_TOKENS or lengths != [item['token_count'] for item in batch]:
                raise ValueError('임베딩 입력 길이가 청크 설정과 다릅니다.')
            inputs = {key: value.to(device) for key, value in inputs.items()}
            with torch.inference_mode():
                hidden = model(**inputs).last_hidden_state
                pooled = masked_mean_pool(hidden, inputs['attention_mask'])
                pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
            result = pooled.cpu().numpy().astype(np.float32)
            if (result.shape != (len(batch), DIMENSION) or not np.isfinite(result).all()
                    or not np.allclose(np.linalg.norm(result, axis=1), 1, atol=1e-4)):
                raise ValueError('벡터 차원·유한값·정규화 검증에 실패했습니다.')
            for chunk, vector in zip(batch, result):
                chunk.update(embedding_status='embedded', vector_row=len(vectors))
                vectors.append(vector)
        except Exception as error:
            message = safe_error(error)
            for chunk in batch:
                chunk.update(embedding_status='failed', embedding_error=message, vector_row=None)
            errors.append(dict(stage='embedding', path=None, code='embedding_batch_failed', message=message))
            if device == 'cuda':
                torch.cuda.empty_cache()
    array = np.stack(vectors) if vectors else np.empty((0, DIMENSION), dtype=np.float32)
    return array, errors
