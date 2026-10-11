import time
from .encoder import cached_source, select_device
from .prompts import MAX_NEW_TOKENS, PROMPT_VERSION, build_context, check_citations

MODEL_ID = 'Qwen/Qwen3-4B-Instruct-2507'
REVISION = 'cdbee75f17c01a7cc42f958dc650907174af0554'


def generator_dtype(torch,device):
    return (torch.bfloat16 if device == 'cuda' and torch.cuda.is_bf16_supported()
            else torch.float16 if device == 'cuda' else torch.float32)


class Generator:
    def __init__(self, settings):
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        self.device = select_device(settings.device, torch)
        dtype = generator_dtype(torch,self.device)
        self.dtype = str(dtype).removeprefix('torch.')
        source = cached_source(MODEL_ID, REVISION, settings.cache)
        kwargs = dict(local_files_only=True, trust_remote_code=False, token=False)
        self.tokenizer = AutoTokenizer.from_pretrained(source, **kwargs)
        self.model = AutoModelForCausalLM.from_pretrained(source, dtype=dtype, **kwargs).to(self.device).eval()

    def answer(self, question, sources):
        import torch
        started = time.perf_counter()
        messages, inputs, allowed = build_context(self.tokenizer, question, sources)
        input_tokens = inputs['input_ids'].shape[-1]
        inputs = {k:v.to(self.device) for k,v in inputs.items()}
        prepared = time.perf_counter()
        with torch.inference_mode():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=MAX_NEW_TOKENS,
                                         pad_token_id=self.tokenizer.eos_token_id)
        done = time.perf_counter()
        new = output[0,input_tokens:].tolist()
        answer = self.tokenizer.decode(new, skip_special_tokens=True).strip()
        eos = self.model.generation_config.eos_token_id
        eos = eos if isinstance(eos,list) else [eos]
        cited, invalid = check_citations(answer, allowed)
        return dict(answer_status='invalid_citation' if invalid else 'generated',answer=answer,
            context_source_ids=allowed,cited_source_ids=cited,invalid_citation_ids=invalid,
            generation=dict(model_id=MODEL_ID,revision=REVISION,prompt_version=PROMPT_VERSION,
                input_tokens=input_tokens,output_tokens=len(new),finish_reason='eos' if new and new[-1] in eos else 'length')), messages, dict(context=(prepared-started)*1000,generation=(done-prepared)*1000)
