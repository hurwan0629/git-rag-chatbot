"""고정 예제로 수집·청킹·벡터 연결·HTML을 검증한다. 네트워크 호출 없음."""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repo_ingestion.chunker import chunk_document
from repo_ingestion.collector import collect, excluded_reason
from repo_ingestion.config import DIMENSION, load_env, parse_repo_url, validate_path
from repo_ingestion.embedder import embed_chunks, masked_mean_pool
from repo_ingestion.github_client import APIError, GitHubClient
from repo_ingestion.report import build_report
from repo_ingestion.run import execute, initial_manifest, make_parser
from repo_ingestion.storage import read_jsonl, stable_id, validate_dataset

COMMIT, TREE, BLOB, REVISION = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40


class CharacterTokenizer:
    """토큰 예산을 재현 가능하게 검증하는 테스트 전용 tokenizer."""
    def __call__(self, text, **kwargs):
        def ids(value):
            return [1] + [ord(char) % 99 + 3 for char in value] + [2]
        if isinstance(text, str):
            return {'input_ids': ids(text)}
        import torch
        rows = [ids(value) for value in text]
        size = max(map(len, rows))
        return {'input_ids': torch.tensor([row + [0] * (size - len(row)) for row in rows]),
                'attention_mask': torch.tensor([[1] * len(row) + [0] * (size - len(row)) for row in rows])}


class ConstantEncoder:
    def __call__(self, input_ids, attention_mask):
        import torch
        shape = (*input_ids.shape, DIMENSION)
        hidden = torch.arange(1, DIMENSION + 1, device=input_ids.device, dtype=torch.float32).expand(shape)
        return SimpleNamespace(last_hidden_state=hidden)


def entry(path, **kwargs):
    return dict(path=path, type='blob', mode='100644', sha=BLOB, **kwargs)


def document(text):
    import hashlib
    data = text.encode('utf-8')
    return dict(document_id=stable_id(['example/repo', COMMIT, 'README.md']), repo='example/repo',
                commit_sha=COMMIT, path='README.md', blob_sha=BLOB, content_sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=len(data), encoding='utf-8', text=text,
                line_count=len(text.splitlines(keepends=True)),
                source_url=f'https://github.com/example/repo/blob/{COMMIT}/README.md')


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.args = make_parser().parse_args(['https://github.com/example/repo'])
        self.settings = initial_manifest('example/repo', self.args, None)['filter']

    def test_input_validation(self):
        self.assertEqual(parse_repo_url('https://github.com/Example/Repo.git'), 'example/repo')
        for value in ['http://github.com/a/b', 'https://github.com/a/b/tree/main',
                      'https://token@github.com/a/b', 'https://github.com/a/b?token=x']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_repo_url(value)
        for path in ['../x', '/x', 'C:/x', 'docs//x', 'x\\y', 'x\x00']:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_path(path)

    def test_env_alias_priority_and_existing_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / '.env'
            env.write_text('GITHUG_TOKEN=file-token\nGITHUB_TOKEN=other-token\n', encoding='utf-8')
            with patch.dict(os.environ, {'GITHUG_TOKEN': 'process-token'}, clear=True):
                self.assertEqual(load_env(env), 'process-token')
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(load_env(env), 'file-token')
            with self.assertRaises(ValueError):
                load_env(Path(tmp) / 'missing')

    def test_filters_prioritize_credentials_and_allow_text_executable(self):
        cases = [('README', None), ('src/a.py', None), ('node_modules/a.py', 'excluded_directory'),
                 ('image.png', 'unsupported_extension'), ('a.key', 'credential_file'), ('.env.local', 'credential_file')]
        for path, reason in cases:
            with self.subTest(path=path):
                self.assertEqual(excluded_reason(entry(path), '', self.settings), reason)
        self.assertIsNone(excluded_reason(dict(entry('script.py'), mode='100755'), '', self.settings))
        self.assertEqual(excluded_reason(entry('docs2/a.md'), 'docs', self.settings), 'outside_scope')
        self.assertEqual(excluded_reason(dict(entry('a.md'), mode='120000'), '', self.settings), 'symlink')
        self.assertEqual(excluded_reason(entry('a.md', size=1000001), '', self.settings), 'too_large')

    def test_expanded_code_defaults_keep_validation_and_explicit_override(self):
        for path in ['src/a.ts', 'ui/a.tsx', 'ui/a.jsx', 'script.mjs', 'config.yaml', 'package.json',
                     'Main.kt', 'build.gradle', 'app.properties', 'Dockerfile', '.gitignore']:
            with self.subTest(path=path):
                self.assertIsNone(excluded_reason(entry(path), '', self.settings))
        for path, reason in [('image.png', 'unsupported_extension'), ('data.ipynb', 'unsupported_extension'),
                              ('.env.production', 'credential_file'), ('node_modules/a.ts', 'excluded_directory')]:
            with self.subTest(path=path):
                self.assertEqual(excluded_reason(entry(path), '', self.settings), reason)
        args = make_parser().parse_args(['https://github.com/example/repo', '--extensions', '.py,.md'])
        narrowed = initial_manifest('example/repo', args, None)['filter']
        self.assertEqual(excluded_reason(entry('a.ts'), '', narrowed), 'unsupported_extension')

    def test_truncated_inventory_walks_subtrees(self):
        client = GitHubClient('example/repo')
        subtree = 'e' * 40
        with patch.object(client, 'tree', side_effect=[
            ([entry('partial.py')], True),
            ([dict(path='docs', type='tree', mode='040000', sha=subtree), entry('README')], False),
            ([entry('한글.md')], False),
        ]) as tree:
            found, complete, errors = client.inventory(TREE)
        self.assertTrue(complete)
        self.assertFalse(errors)
        self.assertEqual({item['path'] for item in found}, {'docs', 'docs/한글.md', 'README'})
        self.assertEqual(tree.call_args_list[-1].args, (subtree,))

    def test_subtree_failure_does_not_claim_complete_inventory(self):
        client = GitHubClient('example/repo')
        with patch.object(client, 'tree', side_effect=[([], True), APIError('network_error', 'offline')]):
            _, complete, errors = client.inventory(TREE)
        self.assertFalse(complete)
        self.assertEqual(errors[0]['code'], 'network_error')

    def test_blob_validates_sha_size_and_base64(self):
        client = GitHubClient('example/repo')
        payload = dict(sha=BLOB, size=3, encoding='base64', content=' YWJj\n')
        with patch.object(client, 'get', return_value=payload):
            self.assertEqual(client.blob(BLOB, 3, 100), b'abc')
            with self.assertRaises(APIError):
                client.blob(BLOB, 4, 100)
        for mutation in [dict(sha=COMMIT), dict(content='%%%'), dict(size=-1), dict(encoding='none')]:
            with self.subTest(mutation=mutation), patch.object(client, 'get', return_value=dict(payload, **mutation)), self.assertRaises(APIError):
                client.blob(BLOB, 3, 100)

    def test_http_auth_and_rate_limit_errors_are_safe_and_distinct(self):
        client = GitHubClient('example/repo', 'test-secret-token')
        for status, headers, code in [(401, {}, 'authentication_error'),
                                       (403, {'X-RateLimit-Remaining': '0'}, 'rate_limit')]:
            error = HTTPError(client.base, status, 'test-secret-token', headers, None)
            with self.subTest(status=status), patch('repo_ingestion.github_client.urlopen', side_effect=error):
                with self.assertRaises(APIError) as caught:
                    client.get('')
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.fatal)
                self.assertNotIn('test-secret-token', str(caught.exception))

    def test_collect_preserves_utf8_bom_crlf_and_exclusion_reasons(self):
        text = '\ufeff한글🙂\r\n두 번째 줄\r\n'
        client = GitHubClient('example/repo')
        with patch.object(client, 'blob', side_effect=[text.encode('utf-8'), b'abc\x00', b'\xff']):
            files, docs = collect(client, [entry('a.md'), entry('b.txt'), entry('c.txt'), entry('.env')],
                                  'example/repo', COMMIT, 'run', '', self.settings)
        self.assertEqual(docs[0]['text'], text)
        self.assertEqual(len(docs), 1)
        self.assertEqual({row['reason_code'] for row in files if row['status'] == 'excluded'},
                         {'credential_file', 'binary_content', 'unsupported_encoding'})

    def test_auth_failure_marks_remaining_files_aborted(self):
        client = GitHubClient('example/repo')
        with patch.object(client, 'blob', side_effect=APIError('rate_limit', 'limited', fatal=True)) as blob:
            files, docs = collect(client, [entry('a.md'), entry('b.md')], 'example/repo', COMMIT, 'run', '', self.settings)
        self.assertFalse(docs)
        self.assertEqual([row['reason_code'] for row in files], ['rate_limit', 'run_aborted'])
        self.assertEqual(blob.call_count, 1)

    def test_chunk_boundaries_tokens_and_unicode_preserve_entire_document(self):
        for text in ['', '한글🙂\r\n짧은 줄\r\n', '한글🙂' * 6000, '\n' * 700, 'a' * 503]:
            with self.subTest(length=len(text)):
                doc = document(text)
                chunks = chunk_document(doc, CharacterTokenizer(), REVISION)
                self.assertEqual(''.join(chunk['text'] for chunk in chunks), text)
                offset = 0
                for index, chunk in enumerate(chunks):
                    self.assertEqual(chunk['start_offset'], offset)
                    self.assertEqual(chunk['chunk_index'], index)
                    self.assertEqual(chunk['text'], text[offset:chunk['end_offset']])
                    self.assertLessEqual(chunk['token_count'], 512)
                    offset = chunk['end_offset']

    def test_masked_mean_excludes_padding(self):
        import torch
        hidden = torch.tensor([[[2., 4.], [4., 8.], [1000., 1000.]]])
        result = masked_mean_pool(hidden, torch.tensor([[1, 1, 0]]))
        self.assertTrue(torch.equal(result, torch.tensor([[3., 6.]])))

    def test_failed_embedding_batch_does_not_consume_vector_rows(self):
        doc = document('x' * 1100)
        chunks = chunk_document(doc, CharacterTokenizer(), REVISION)
        encoder = ConstantEncoder()
        with patch.object(encoder.__class__, '__call__', side_effect=[RuntimeError('first batch failed'),
              SimpleNamespace(last_hidden_state=__import__('torch').ones(1, chunks[1]['token_count'], DIMENSION)),
              SimpleNamespace(last_hidden_state=__import__('torch').ones(1, chunks[2]['token_count'], DIMENSION))]):
            vectors, errors = embed_chunks(chunks, CharacterTokenizer(), encoder, 'cpu', 1)
        self.assertEqual(vectors.shape, (2, DIMENSION))
        self.assertEqual([chunk['vector_row'] for chunk in chunks], [None, 0, 1])
        self.assertEqual(len(errors), 1)

    def run_fixture(self, output, collect_only=False):
        args = make_parser().parse_args(['https://github.com/example/repo'] + (['--collect-only'] if collect_only else []))
        source = '# 한글🙂\r\n</script><img src=x onerror=alert(1)>\r\n' + 'x' * 700
        with patch('repo_ingestion.github_client.GitHubClient.snapshot', return_value=(COMMIT, TREE)), \
             patch('repo_ingestion.github_client.GitHubClient.inventory', return_value=([entry('README.md')], True, [])), \
             patch('repo_ingestion.github_client.GitHubClient.blob', return_value=source.encode('utf-8')), \
             patch('repo_ingestion.embedder.resolve_revision', return_value=REVISION), \
             patch('repo_ingestion.embedder.load_tokenizer', return_value=CharacterTokenizer()), \
             patch('repo_ingestion.embedder.load_encoder', return_value=(ConstantEncoder(), 'cpu')):
            with patch('sys.stdout', new=io.StringIO()):
                code = execute(args, 'example/repo', None, output)
        self.assertEqual(code, 0)
        path = max(output.glob('*/*/manifest.json'), key=lambda p: p.stat().st_mtime_ns)
        return path.parent, json.loads(path.read_text(encoding='utf-8'))

    def test_full_pipeline_storage_validation_and_safe_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            folder, manifest = self.run_fixture(output)
            self.assertEqual(manifest['status'], 'completed')
            _, _, chunks = validate_dataset(folder, manifest, CharacterTokenizer())
            self.assertTrue(all(chunk['embedding_status'] == 'embedded' for chunk in chunks))
            html = (output / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('</script><img', html)
            self.assertIn('\\u003c/script\\u003e', html)
            self.assertIn('Array.from(doc.text)', html)
            # 벡터 행 참조를 바꿨을 때 저장 검증이 실패하는지 확인한다.
            chunks[0]['vector_row'] = len(chunks) + 5
            from repo_ingestion.storage import write_jsonl
            write_jsonl(folder / 'chunks.jsonl', chunks)
            with self.assertRaisesRegex(ValueError, 'vector 행'):
                validate_dataset(folder, manifest)

    def test_collect_only_and_multiple_runs_share_single_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            _, first = self.run_fixture(output)
            folder, second = self.run_fixture(output, collect_only=True)
            self.assertEqual(second['status'], 'collected_only')
            self.assertFalse((folder / 'vectors.npy').exists())
            self.assertTrue(all(chunk['vector_row'] is None for chunk in read_jsonl(folder / 'chunks.jsonl')))
            self.assertEqual(len(list(output.rglob('*.html'))), 1)
            html = (output / 'index.html').read_text(encoding='utf-8')
            self.assertIn(first['run_id'], html)
            self.assertIn(second['run_id'], html)

    def test_model_failure_keeps_original_and_reports_partial(self):
        args = make_parser().parse_args(['https://github.com/example/repo'])
        with tempfile.TemporaryDirectory() as tmp, \
             patch('repo_ingestion.github_client.GitHubClient.snapshot', return_value=(COMMIT, TREE)), \
             patch('repo_ingestion.github_client.GitHubClient.inventory', return_value=([entry('README.md')], True, [])), \
             patch('repo_ingestion.github_client.GitHubClient.blob', return_value=b'hello'), \
             patch('repo_ingestion.embedder.resolve_revision', side_effect=RuntimeError('model unavailable')), \
             patch('sys.stdout', new=io.StringIO()), patch('sys.stderr', new=io.StringIO()):
            output = Path(tmp)
            self.assertEqual(execute(args, 'example/repo', None, output), 1)
            path = next(output.glob('*/*/manifest.json'))
            manifest = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(manifest['status'], 'partial')
            self.assertEqual(read_jsonl(path.parent / 'documents.jsonl')[0]['text'], 'hello')
            self.assertTrue((output / 'index.html').exists())

    def test_report_survives_corrupted_old_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            path = output / 'bad-repo' / 'run' / 'manifest.json'
            path.parent.mkdir(parents=True)
            path.write_text('{bad', encoding='utf-8')
            html, warnings = build_report(output)
            self.assertTrue(html.exists())
            self.assertEqual(len(warnings), 1)


if __name__ == '__main__':
    unittest.main()
