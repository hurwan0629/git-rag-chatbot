"""고정한 Qwen revision을 CUDA/BF16으로 로딩해 새 토큰만 생성한다."""

import json
import re
import time
from pathlib import Path

from repo_ingestion.storage import write_json
from . import APIError
from .prompts import MAX_NEW_TOKENS, PROMPT_VERSION, build_context, check_citations

MODEL_ID = 'Qwen/Qwen3-4B-Instruct-2507'
CACHE_DIR = Path(__file__).resolve().parent / '.model-cache'


def resolve_revision(cache=CACHE_DIR):
    pin = Path(cache) / 'model.json'
    if pin.is_file():
        saved = json.loads(pin.read_text(encoding='utf-8'))
        revision = saved.get('revision')
        if saved.get('model_id') != MODEL_ID or not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{40}', revision):
            raise ValueError('Qwen revision 고정 파일이 유효하지 않습니다.')
        return revision
    from huggingface_hub import HfApi
    revision = HfApi(token=False).model_info(MODEL_ID).sha
    if not revision or not re.fullmatch(r'[a-f0-9]{40}', revision):
        raise ValueError('Qwen revision SHA를 확인하지 못했습니다.')
    write_json(pin, dict(model_id=MODEL_ID, revision=revision))
    return revision


class Generator:
    def __init__(self):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise ValueError('답변 생성은 CUDA/BF16이 필요합니다. 검색만 확인하려면 --search-only를 사용하세요.')
        self.revision = resolve_revision()
        self.device = 'cuda'
        kwargs = dict(revision=self.revision, cache_dir=str(CACHE_DIR), token=False, trust_remote_code=False)
        self.tokenizer = AutoTokenizer.from_pretrained(MODEL_ID, **kwargs)
        self.model = AutoModelForCausalLM.from_pretrained(MODEL_ID, dtype=torch.bfloat16, **kwargs).to(self.device).eval()

    def answer(self, question, sources):
        import torch
        started = time.perf_counter()
        messages, inputs, allowed = build_context(self.tokenizer, question, sources)
        input_tokens = inputs['input_ids'].shape[-1]
        inputs = {key: value.to(self.device) for key, value in inputs.items()}
        prepared = time.perf_counter()
        with torch.inference_mode():
            outputs = self.model.generate(**inputs, do_sample=False, max_new_tokens=MAX_NEW_TOKENS,
                                           pad_token_id=self.tokenizer.eos_token_id)
        generated = time.perf_counter()
        new_tokens = outputs[0, input_tokens:].tolist()
        answer = self.tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
        eos_ids = self.model.generation_config.eos_token_id
        eos_ids = eos_ids if isinstance(eos_ids, list) else [eos_ids]
        finish = 'eos' if new_tokens and new_tokens[-1] in eos_ids else 'length'
        cited, invalid = check_citations(answer, allowed)
        result = dict(answer_status='invalid_citation' if invalid else 'generated', answer=answer,
                      context_source_ids=allowed, cited_source_ids=cited, invalid_citation_ids=invalid,
                      generation=dict(model_id=MODEL_ID, revision=self.revision, prompt_version=PROMPT_VERSION,
                                      input_tokens=input_tokens, output_tokens=len(new_tokens), finish_reason=finish))
        return result, messages, dict(context=(prepared-started)*1000, generation=(generated-prepared)*1000)
