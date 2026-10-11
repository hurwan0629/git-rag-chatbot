"""모델 다운로드 없이 실행하는 저장 검증·검색·프롬프트·HTTP 단위 테스트."""

import hashlib
import http.client
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from contextlib import redirect_stderr

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from repo_ingestion.config import CHUNKING, DIMENSION
from repo_ingestion.storage import save_dataset, stable_id, write_json
from retrieval_generation import APIError
from retrieval_generation.catalog import Catalog, EMBEDDING
from retrieval_generation.generator import Generator, resolve_revision
from retrieval_generation.prompts import build_context, check_citations
from retrieval_generation.retriever import Retriever, rank_sources
from retrieval_generation.server import App, main, make_server, validate_input


def unit_vector(column=0):
    vector = np.zeros(DIMENSION, dtype=np.float32)
    vector[column] = 1
    return vector


def make_run(root, repo='alice/demo', run_id='run01', finished='2026-10-10T01:00:00+00:00',
             text='첫째 줄 😀\r\nsecond line\n', column=0):
    folder = Path(root) / repo.replace('/', '--') / run_id
    commit = 'a' * 40
    identifier = stable_id([repo, commit, 'src/main.py'])
    document = dict(document_id=identifier, repo=repo, commit_sha=commit, path='src/main.py',
                    blob_sha='b'*40, content_sha256=hashlib.sha256(text.encode()).hexdigest(),
                    size_bytes=len(text.encode()), encoding='utf-8', text=text,
                    line_count=len(text.splitlines(keepends=True)), source_url='https://example.invalid')
    chunk = dict(chunk_id=stable_id(dict(document_id=identifier, start_offset=0, end_offset=len(text),
                                         model_id=EMBEDDING['model_id'], revision=EMBEDDING['revision'], chunking=CHUNKING)),
                 document_id=identifier, chunk_index=0, start_offset=0, end_offset=len(text),
                 start_line=1, end_line=document['line_count'], text=text, token_count=10,
                 embedding_status='embedded', embedding_error=None, vector_row=0)
    file = dict(path=document['path'], entry_type='blob', mode='100644', git_object_sha=document['blob_sha'],
                size_bytes=document['size_bytes'], status='collected', reason_code=None, reason=None,
                document_id=identifier, chunk_count=1, embedded_chunk_count=1,
                source_url=document['source_url'], view_hash=None)
    manifest = dict(schema_version='1.0', repo=repo, run_id=run_id, commit_sha=commit, mode='embed',
                    status='completed', finished_at=finished, inventory_complete=True,
                    embedding=dict(EMBEDDING), chunking=dict(CHUNKING),
                    counts=dict(listed_files=1, collected_files=1, excluded_files=0,
                                failed_files=0, documents=1, chunks=1, vectors=1))
    save_dataset(folder, [file], [document], [chunk], unit_vector(column).reshape(1, -1))
    write_json(folder / 'manifest.json', manifest)
    return folder, manifest


class FakeTokenizer:
    def __init__(self, special=2):
        self.special, self.calls = special, []

    def __call__(self, text, add_special_tokens=True, return_tensors=None, **kwargs):
        self.calls.append((text, kwargs))
        count = len(text.split()) + (self.special if add_special_tokens else 0)
        if return_tensors:
            import torch
            return dict(input_ids=torch.zeros((1, count), dtype=torch.long),
                        attention_mask=torch.ones((1, count), dtype=torch.long))
        return dict(input_ids=list(range(count)))


class FakeChatTokenizer:
    eos_token_id = 5

    def apply_chat_template(self, messages, **kwargs):
        import torch
        self.messages, self.kwargs = messages, kwargs
        payload = json.loads(messages[1]['content'])
        length = 30 + sum(len(s['text']) for s in payload['sources'])
        return dict(input_ids=torch.zeros((1, length), dtype=torch.long),
                    attention_mask=torch.ones((1, length), dtype=torch.long))

    def decode(self, tokens, **kwargs):
        self.decoded_tokens = tokens
        return '설명 [S1] [S0]'


class FakeRetriever:
    device = 'cpu'

    def __init__(self, catalog, empty=False):
        self.catalog, self.empty = catalog, empty

    def search(self, question, repos, top_k):
        results = [] if self.empty else rank_sources(self.catalog, unit_vector(), repos, top_k)
        return dict(status='ok' if results else 'no_results', question_tokens=4, results=results,
                    timings_ms=dict(query_encoding=1., search=1., context=0., generation=0., total=2.))


class FakeGenerator:
    revision = 'c' * 40

    def answer(self, question, sources):
        return (dict(answer_status='generated', answer='확인한 설명 [S1]',
                     context_source_ids=['S1'], cited_source_ids=['S1'], invalid_citation_ids=[],
                     generation=dict(model_id='mock', revision=self.revision, prompt_version='1.0',
                                     input_tokens=20, output_tokens=5, finish_reason='eos')),
                [dict(role='user', content=question)], dict(context=1., generation=2.))


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_latest_completed_and_tie_break(self):
        make_run(self.root, run_id='run01')
        make_run(self.root, run_id='run02')
        folder, m = make_run(self.root, run_id='run03', finished='2026-10-10T02:00:00+00:00')
        m['status'] = 'failed'
        write_json(folder / 'manifest.json', m)
        catalog = Catalog(self.root)
        self.assertEqual(catalog.repos[0]['run_id'], 'run02')
        self.assertTrue(catalog.repos[0]['searchable'])
        self.assertEqual(len(catalog.skipped_runs), 2)
        self.assertEqual(catalog.catalog_id, Catalog(self.root).catalog_id)

    def test_newest_incompatible_has_no_older_fallback(self):
        make_run(self.root)
        folder, m = make_run(self.root, run_id='run02', finished='2026-10-10T02:00:00+00:00')
        m['embedding']['revision'] = 'd'*40
        write_json(folder / 'manifest.json', m)
        catalog = Catalog(self.root)
        self.assertFalse(catalog.runs)
        self.assertEqual(catalog.repos[0]['run_id'], 'run02')
        self.assertIn('revision', catalog.repos[0]['reason'])

    def test_corrupt_vectors_and_raw_are_rejected(self):
        folder, m = make_run(self.root)
        np.save(folder / 'vectors.npy', np.full((1, DIMENSION), np.nan, dtype=np.float32))
        self.assertFalse(Catalog(self.root).repos[0]['searchable'])
        make_run(self.root)
        next((folder / 'raw').glob('*.txt')).write_text('changed', encoding='utf-8')
        self.assertFalse(Catalog(self.root).repos[0]['searchable'])

    def test_empty_vectors_and_bad_json_are_reported(self):
        folder, m = make_run(self.root)
        m['counts'] = dict(listed_files=0, collected_files=0, excluded_files=0, failed_files=0,
                           documents=0, chunks=0, vectors=0)
        save_dataset(folder, [], [], [], np.empty((0, DIMENSION), dtype=np.float32))
        write_json(folder / 'manifest.json', m)
        self.assertIn('벡터', Catalog(self.root).repos[0]['reason'])
        (folder / 'manifest.json').write_text('{', encoding='utf-8')
        self.assertEqual(len(Catalog(self.root).skipped_runs), 1)

    def test_missing_timestamp_no_fallback(self):
        make_run(self.root)
        folder, m = make_run(self.root, run_id='run02')
        del m['finished_at']
        write_json(folder / 'manifest.json', m)
        self.assertFalse(Catalog(self.root).repos[0]['searchable'])

    def test_document_known_id_only_and_unicode_preserved(self):
        make_run(self.root)
        catalog = Catalog(self.root)
        doc_id = next(iter(catalog.runs['alice/demo']['documents']))
        doc = catalog.document('run01', doc_id)
        self.assertIn('😀\r\n', doc['text'])
        self.assertEqual(doc['source_url'], 'https://github.com/alice/demo/blob/'+'a'*40+'/src/main.py')
        for run, doc_id in [('run01', '../../.env'), ('other', doc_id)]:
            with self.assertRaises(APIError) as ctx:
                catalog.document(run, doc_id)
            self.assertEqual(ctx.exception.status, 404)


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        make_run(self.temp.name)
        make_run(self.temp.name, repo='bob/demo', run_id='run02', column=1)
        self.catalog = Catalog(self.temp.name)

    def test_scope_ranking_and_row_reference(self):
        results = rank_sources(self.catalog, unit_vector(), ['bob/demo'], 5)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['repo'], 'bob/demo')
        self.assertEqual(results[0]['score'], 0)
        results = rank_sources(self.catalog, unit_vector(), ['bob/demo', 'alice/demo'], 5)
        self.assertEqual([s['repo'] for s in results], ['alice/demo', 'bob/demo'])
        self.assertEqual(results[0]['vector_row'], 0)
        self.assertEqual([s['source_id'] for s in results], ['S1', 'S2'])

    def test_tie_sort_and_dedup(self):
        self.catalog.runs['bob/demo']['vectors'][0] = unit_vector()
        results = rank_sources(self.catalog, unit_vector(), ['bob/demo', 'alice/demo'], 5)
        self.assertEqual(results[0]['repo'], 'alice/demo')
        self.catalog.runs['bob/demo']['chunks'][0]['chunk_id'] = results[0]['chunk_id']
        self.assertEqual(len(rank_sources(self.catalog, unit_vector(), ['bob/demo', 'alice/demo'], 5)), 1)

    def test_input_types_and_empty_selection(self):
        valid = dict(question=' 질문 ', repos=['alice/demo'])
        self.assertEqual(validate_input(valid, self.catalog)['question'], '질문')
        for update in [dict(question=' '), dict(question=3), dict(repos=[]), dict(repos='alice/demo'),
                       dict(repos=[[]]), dict(repos=['missing']), dict(repos=['alice/demo']*2),
                       dict(top_k=True), dict(top_k=0), dict(top_k=11), dict(top_k=1.2),
                       dict(top_k='5'), dict(extra=1)]:
            with self.subTest(update=update), self.assertRaises(APIError):
                validate_input(dict(valid, **update), self.catalog)

    def test_token_boundaries_and_prefix(self):
        retriever = Retriever.__new__(Retriever)
        retriever.tokenizer = FakeTokenizer(special=11)
        self.assertEqual(retriever.measure_question('word '*500), 500)
        self.assertTrue(retriever.tokenizer.calls[-1][0].startswith('query: '))
        self.assertFalse(retriever.tokenizer.calls[-1][1]['truncation'])
        for count, special in [(501, 2), (500, 12)]:
            retriever.tokenizer = FakeTokenizer(special)
            with self.assertRaises(APIError) as ctx:
                retriever.measure_question('word '*count)
            self.assertEqual(ctx.exception.status, 422)

    def test_encoder_prefix_and_normalization(self):
        import torch
        retriever = Retriever.__new__(Retriever)
        retriever.device, retriever.tokenizer = 'cpu', FakeTokenizer()
        def forward(**inputs):
            hidden = torch.zeros((*inputs['input_ids'].shape, DIMENSION))
            hidden[:, :, 0], hidden[:, :, 1] = 3, 4
            return SimpleNamespace(last_hidden_state=hidden)
        retriever.model = forward
        vector = retriever.encode('hello')
        np.testing.assert_allclose(vector[:2], [.6, .8])
        self.assertEqual(retriever.tokenizer.calls[-1][0], 'query: hello')
        self.assertAlmostEqual(float(np.linalg.norm(vector)), 1)


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        make_run(self.temp.name)
        self.sources = rank_sources(Catalog(self.temp.name), unit_vector(), ['alice/demo'], 5)

    def test_budget_drops_whole_low_ranked_source(self):
        tokenizer = FakeChatTokenizer()
        self.sources[0]['text'] = 'x'*20
        self.sources.append(dict(self.sources[0], source_id='S2', text='x'*60))
        messages, inputs, allowed = build_context(tokenizer, '원래 질문', self.sources, budget=60)
        self.assertEqual(allowed, ['S1'])
        self.assertEqual(inputs['input_ids'].shape[-1], 50)
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['question'], '원래 질문')
        self.assertNotIn('query: ', messages[1]['content'])
        self.assertEqual(payload['sources'][0]['text'], 'x'*20)
        self.assertTrue(tokenizer.kwargs['add_generation_prompt'])
        self.assertTrue(tokenizer.kwargs['tokenize'])

    def test_budget_rejects_when_one_source_cannot_fit(self):
        with self.assertRaises(APIError) as ctx:
            build_context(FakeChatTokenizer(), 'question', self.sources, budget=1)
        self.assertEqual(ctx.exception.code, 'CONTEXT_TOO_LARGE')

    def test_citation_validation_includes_zero_and_dropped_ids(self):
        valid, invalid = check_citations('[S1] [S1, S2] [S0]', ['S1'])
        self.assertEqual(valid, ['S1'])
        self.assertEqual(invalid, ['S2', 'S0'])

    def test_generator_decodes_only_new_tokens(self):
        import torch
        generator = Generator.__new__(Generator)
        generator.tokenizer, generator.device, generator.revision = FakeChatTokenizer(), 'cpu', 'c'*40
        def generate(**kwargs):
            self.assertFalse(kwargs['do_sample'])
            self.assertEqual(kwargs['max_new_tokens'], 512)
            return torch.cat([kwargs['input_ids'], torch.tensor([[44, 5]])], dim=1)
        generator.model = SimpleNamespace(generate=generate, generation_config=SimpleNamespace(eos_token_id=[5]))
        result, messages, times = generator.answer('question', self.sources)
        self.assertEqual(generator.tokenizer.decoded_tokens, [44, 5])
        self.assertEqual(result['generation']['output_tokens'], 2)
        self.assertEqual(result['generation']['finish_reason'], 'eos')
        self.assertEqual(result['answer_status'], 'invalid_citation')

    def test_cached_generator_revision_does_not_call_network(self):
        write_json(Path(self.temp.name) / 'model.json', dict(model_id='Qwen/Qwen3-4B-Instruct-2507', revision='f'*40))
        self.assertEqual(resolve_revision(Path(self.temp.name)), 'f'*40)
        write_json(Path(self.temp.name) / 'model.json', dict(model_id='wrong', revision='f'*40))
        with self.assertRaises(ValueError):
            resolve_revision(Path(self.temp.name))


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        make_run(self.temp.name)
        self.catalog = Catalog(self.temp.name)
        self.app = App(self.catalog, FakeRetriever(self.catalog), FakeGenerator(), Path(self.temp.name)/'logs')
        self.payload = dict(question='질문', repos=['alice/demo'])

    def test_answer_and_saved_prompt(self):
        status, data = self.app.post('/api/answer', self.payload, 'req01')
        self.assertEqual(status, 200)
        self.assertEqual(data['context_source_ids'], ['S1'])
        line = json.loads((self.app.session_dir / 'requests.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(line['messages'][0]['content'], '질문')
        self.assertEqual(line['response'], data)
        session = json.loads((self.app.session_dir / 'session.json').read_text(encoding='utf-8'))
        self.assertEqual(session['mode'], 'rag')

    def test_no_results_does_not_generate(self):
        self.app.retriever.empty = True
        self.app.generator.answer = Mock(side_effect=AssertionError('must not run'))
        status, data = self.app.post('/api/answer', self.payload, 'req01')
        self.assertEqual(data['answer_status'], 'no_evidence')
        self.app.generator.answer.assert_not_called()

    def test_disabled_and_busy(self):
        self.app.generator = None
        status, _ = self.app.post('/api/answer', self.payload, 'req01')
        self.assertEqual(status, 503)
        self.app.work_lock.acquire()
        try:
            status, _ = self.app.post('/api/search', self.payload, 'req02')
            self.assertEqual(status, 409)
        finally:
            self.app.work_lock.release()

    def test_failure_releases_lock_and_records_error(self):
        self.app.retriever.search = Mock(side_effect=APIError(422, 'QUESTION_TOO_LONG', 'too long'))
        status, _ = self.app.post('/api/search', self.payload, 'req01')
        self.assertEqual(status, 422)
        self.assertFalse(self.app.work_lock.locked())
        line = json.loads((self.app.session_dir / 'requests.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(line['response']['error']['code'], 'QUESTION_TOO_LONG')

    def test_logging_failure_is_reported(self):
        with patch.object(self.app, 'record', side_effect=APIError(500, 'LOG_WRITE_FAILED', 'failed')):
            status, data = self.app.post('/api/search', self.payload, 'req01')
        self.assertEqual(status, 500)
        self.assertEqual(data['error']['code'], 'LOG_WRITE_FAILED')
        self.assertFalse(self.app.work_lock.locked())


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        make_run(cls.temp.name)
        cls.catalog = Catalog(cls.temp.name)
        cls.app = App(cls.catalog, FakeRetriever(cls.catalog), output_dir=Path(cls.temp.name)/'logs')
        cls.server = make_server(cls.app, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        status, content_type, raw = response.status, response.getheader('Content-Type'), response.read()
        connection.close()
        return status, content_type, raw

    def test_get_html_catalog_and_known_document(self):
        status, content_type, raw = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('text/html', content_type)
        self.assertIn('Search & Answer', raw.decode())
        status, _, raw = self.request('GET', '/api/repos')
        data = json.loads(raw)
        self.assertEqual(data['mode'], 'search_only')
        doc_id = next(iter(self.catalog.runs['alice/demo']['documents']))
        status, content_type, raw = self.request('GET', '/api/document?run_id=run01&document_id='+doc_id)
        self.assertEqual(status, 200)
        self.assertIn('😀', json.loads(raw)['text'])
        self.assertEqual(content_type, 'application/json; charset=utf-8')

    def test_post_search_and_disabled_answer(self):
        body = json.dumps(dict(question='질문', repos=['alice/demo'])).encode()
        headers = {'Content-Type': 'application/json'}
        status, _, raw = self.request('POST', '/api/search', body, headers)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['results'][0]['repo'], 'alice/demo')
        status, _, raw = self.request('POST', '/api/answer', body, headers)
        self.assertEqual(status, 503)

    def test_invalid_json_limits_and_unicode(self):
        for body, expected in [(b'{',400), (b'x'*16385,413), (b'{"question":NaN}',400),
                               (b'{"question":"\\ud800"}',400), (b'[[]]',400)]:
            with self.subTest(body=body[:30]):
                status, _, raw = self.request('POST', '/api/search', body, {'Content-Type':'application/json'})
                self.assertEqual(status, expected)
                self.assertEqual(json.loads(raw)['status'], 'error')
        status, _, _ = self.request('POST', '/api/search', b'{}', {'Content-Type':'text/plain'})
        self.assertEqual(status, 400)

    def test_unknown_paths_and_document_ids(self):
        for path in ['/../../.env', '/api/document?run_id=run01&document_id=../../.env',
                     '/api/document?run_id=run01&run_id=run02&document_id=none']:
            status, _, _ = self.request('GET', path)
            self.assertEqual(status, 404)

    def test_port_collision_fails_before_catalog_or_model_loading(self):
        with patch('retrieval_generation.server.Catalog') as loader, redirect_stderr(io.StringIO()):
            self.assertEqual(main(['--port', str(self.server.server_port), '--search-only']), 1)
        loader.assert_not_called()


if __name__ == '__main__':
    unittest.main()
