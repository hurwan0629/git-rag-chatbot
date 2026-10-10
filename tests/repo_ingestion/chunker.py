"""줄 경계를 우선하며 최종 모델 입력 길이와 원문 문자 offset을 보존한다."""

from .config import CHUNKING, MAX_INPUT_TOKENS, MODEL_ID, PREFIX
from .storage import line_range, line_starts, stable_id


def chunk_document(document, tokenizer, revision):
    text = document['text']
    if not text:
        return []
    starts = line_starts(text)
    ends, offset = [], 0
    for line in text.splitlines(keepends=True):
        offset += len(line)
        ends.append(offset)

    def count(start, end):
        return len(tokenizer(PREFIX + text[start:end], add_special_tokens=True,
                             truncation=False, verbose=False)['input_ids'])

    spans, start, line_index = [], 0, 0
    while start < len(text):
        while ends[line_index] <= start:
            line_index += 1
        end = start
        scan = line_index
        # 매우 긴 줄을 매 청크마다 통째로 재인코딩하지 않도록 검사할 문자 구간도 제한한다.
        char_budget = MAX_INPUT_TOKENS * 16
        while (scan < len(ends) and ends[scan] - start <= char_budget
               and count(start, ends[scan]) <= MAX_INPUT_TOKENS):
            end = ends[scan]
            scan += 1
        if end == start:
            # tokenizer 길이는 문자 수와 같지 않다. 실제 길이를 검사한 구간만 채택한다.
            low, high, end = start + 1, min(ends[line_index], start + char_budget), start
            while low <= high:
                middle = (low + high) // 2
                if count(start, middle) <= MAX_INPUT_TOKENS:
                    end, low = middle, middle + 1
                else:
                    high = middle - 1
            if end == start:
                raise ValueError('문자 하나도 모델 입력 예산에 들어가지 않습니다.')
        spans.append((start, end))
        start = end
    chunks = []
    for index, (start, end) in enumerate(spans):
        first, last = line_range(starts, start, end)
        chunks.append(dict(chunk_id=stable_id(dict(document_id=document['document_id'], start_offset=start,
                                                   end_offset=end, model_id=MODEL_ID, revision=revision,
                                                   chunking=CHUNKING)),
                           document_id=document['document_id'], chunk_index=index,
                           start_offset=start, end_offset=end, start_line=first, end_line=last,
                           text=text[start:end], token_count=count(start, end),
                           embedding_status='not_run', embedding_error=None, vector_row=None))
    return chunks
