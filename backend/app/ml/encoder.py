import threading
import numpy as np
from ..config import ROOT
from ..errors import APIError
from ..services.storage import stable_id

MODEL_ID = 'intfloat/multilingual-e5-small'
REVISION = '614241f622f53c4eeff9890bdc4f31cfecc418b3'
PROFILE = dict(model_id=MODEL_ID, revision=REVISION, dimension=384,
    passage_prefix='passage: ', query_prefix='query: ', pooling='masked_mean',
    normalize=True, dtype='float32', max_input_tokens=512)
PROFILE_ID = stable_id(PROFILE)


def masked_mean_pool(hidden, attention_mask):
    mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
    return (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1)


def select_device(requested, torch):
    if requested == 'cuda' and not torch.cuda.is_available():
        raise ValueError('CUDA is unavailable; use MODEL_DEVICE=cpu or auto')
    return ('cuda' if torch.cuda.is_available() else 'cpu') if requested == 'auto' else requested


def cached_source(model_id, revision, cache):
    """Reuse complete prototype caches without copying several GB of weights."""
    from huggingface_hub import try_to_load_from_cache
    from pathlib import Path
    caches = [cache, ROOT / 'tests/repo_ingestion/.model-cache',
              ROOT / 'tests/retrieval_generation/.model-cache']
    for candidate in caches:
        config = try_to_load_from_cache(model_id, 'config.json', revision=revision, cache_dir=str(candidate))
        if isinstance(config,str):
            return str(Path(config).parent)
    raise ValueError('Model is not cached. Run: python backend/manage.py download-models')


class Encoder:
    def __init__(self, settings):
        import torch
        from transformers import AutoModel, AutoTokenizer
        self.device = select_device(settings.device, torch)
        source = cached_source(MODEL_ID, REVISION, settings.cache)
        kwargs = dict(local_files_only=True, trust_remote_code=False, token=False)
        self.tokenizer = AutoTokenizer.from_pretrained(source, **kwargs)
        self.model = AutoModel.from_pretrained(source, dtype=torch.float32, **kwargs).to(self.device).eval()
        if self.model.config.hidden_size != 384:
            raise ValueError('Unexpected E5 dimension')
        self.tokenizer_lock = threading.Lock()

    def question_tokens(self, question):
        with self.tokenizer_lock:
            count = len(self.tokenizer(question, add_special_tokens=False, truncation=False)['input_ids'])
            full = len(self.tokenizer('query: ' + question, truncation=False)['input_ids'])
        if count > 500 or full > 512:
            raise APIError(422, 'QUESTION_TOO_LONG', '질문은 E5 본문 500 token, 전체 입력 512 token 이하여야 합니다.')
        return count

    def encode(self, texts, prefix):
        import torch
        with self.tokenizer_lock:
            inputs = self.tokenizer([prefix + t for t in texts], padding=True,
                                    truncation=False, return_tensors='pt')
        if inputs['input_ids'].shape[-1] > 512:
            raise ValueError('E5 input exceeds 512 tokens')
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        with torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            vectors = torch.nn.functional.normalize(masked_mean_pool(hidden, inputs['attention_mask']), p=2, dim=1)
        result = vectors.cpu().numpy().astype(np.float32)
        if result.shape != (len(texts), 384) or not np.isfinite(result).all() or not np.allclose(np.linalg.norm(result,axis=1),1,atol=1e-4):
            raise ValueError('Invalid E5 output')
        return result
