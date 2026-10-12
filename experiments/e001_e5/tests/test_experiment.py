import math
from datetime import datetime
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
from common import ROOT, digest, file_hash, load_config, prepared_data, protocol_hash, write_json, write_jsonl
from prepare import bounded_query, clean_code, evaluation_rows, pairs, snapshot_files, source_rows
from retrieval import file_ranking, query_metrics


class Tokenizer:
    def __call__(self,text,add_special_tokens=True,**kwargs):
        return dict(input_ids=[ord(c) for c in text]+([0,0] if add_special_tokens else []))
    def decode(self,ids,**kwargs):return ''.join(chr(c) for c in ids)


class ExperimentTests(unittest.TestCase):
    def test_metrics_multi_positive_and_missing(self):
        ks=dict(recall_at_k=[2],mrr_at_k=[3],ndcg_at_k=[3])
        result=query_metrics(['x','a','b'],['a','b'],ks)
        self.assertEqual(result['recall_at_2'],.5);self.assertEqual(result['mrr_at_3'],.5)
        expected=(1/math.log2(3)+1/math.log2(4))/(1+1/math.log2(3))
        self.assertAlmostEqual(result['ndcg_at_3'],expected)
        self.assertEqual(query_metrics(['x'],['a'],ks)['mrr_at_3'],0)
        with self.assertRaises(ValueError):query_metrics(['a','a'],['a'],ks)

    def test_file_max_aggregation_before_top_k(self):
        def chunk(path,id):return dict(path=path,chunk_id=id,start_line=1,end_line=1,text=id)
        result=file_ranking([chunk('a.py','1'),chunk('a.py','2'),chunk('b.py','3')],[.9,.8,.7],2)
        self.assertEqual([r['path'] for r in result],['a.py','b.py'])
        self.assertEqual(result[0]['chunk_id'],'1')

    def test_docstring_removal_preserves_function(self):
        code,canonical=clean_code('def add(a,b):\n    """Add numbers. More detail."""\n    return a+b','Add numbers.')
        self.assertNotIn('Add numbers',code);self.assertIn('return a + b',code)
        import ast
        self.assertIsNone(ast.get_docstring(ast.parse(code).body[0]))

    def test_query_head_tail_and_length(self):
        config=load_config();text='a'*1000+'end'
        query,count,truncated=bounded_query(text,Tokenizer(),config)
        self.assertTrue(truncated);self.assertEqual(count,1003);self.assertTrue(query.endswith('end'))
        self.assertLessEqual(len(Tokenizer()('query: '+query)['input_ids']),512)

    def test_sampling_and_cross_split_exclusions(self):
        config=load_config();config['data']['train_pairs']=2
        parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            path=Path(temp)/'train.jsonl'
            records=[dict(repo='fixture/train',path=f'{i}.py',partition='train',language='python',docstring=f'Return value {i}',
                code=f'def f{i}():\n    """Return value {i}"""\n    return {i}') for i in range(5)]
            records.append(records[0]|{'repo':'fixture/eval'})
            write_jsonl(path,records)
            first=pairs(path,'train',config,Tokenizer(),{'fixture/eval'})
            second=pairs(path,'train',config,Tokenizer(),{'fixture/eval'})
            self.assertEqual(first[0],second[0]);self.assertEqual(first[1]['excluded_repository'],1)
            with self.assertRaises(ValueError):pairs(path,'train',config,Tokenizer(),{'fixture/eval'},first[3],first[4])

    def test_prepared_hash_protects_training_input(self):
        config=load_config();parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            path=Path(temp);write_jsonl(path/'train.jsonl',[{'query':'hello'}])
            files={'train.jsonl':file_hash(path/'train.jsonl')}
            write_json(path/'manifest.json',dict(status='completed',protocol_hash=protocol_hash(config),files=files,dataset_id=digest(files)))
            prepared_data(config,path)
            write_jsonl(path/'train.jsonl',[{'query':'changed'}])
            with self.assertRaises(ValueError):prepared_data(config,path)

    def test_archive_traversal_is_rejected_without_extraction(self):
        config=load_config();parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            root=Path(temp);folder=root/'owner__repo';folder.mkdir();sha='a'*40
            with zipfile.ZipFile(folder/(sha+'.zip'),'w') as z:z.writestr('repo-'+sha+'/../escape.py','pass')
            with self.assertRaises(ValueError):list(snapshot_files(root,'owner/repo',sha,False,config))
            self.assertFalse((root/'escape.py').exists())

    def test_official_zip_split_and_gzip_reader(self):
        parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            path=Path(temp)/'python.zip'
            with zipfile.ZipFile(path,'w') as archive:
                for split in ('train','valid'):
                    data=(json.dumps({'partition':split})+'\n').encode()
                    archive.writestr(f'python/final/jsonl/{split}/python_0.jsonl.gz',gzip.compress(data))
            self.assertEqual(list(source_rows(path,'train')),[{'partition':'train'}])
            self.assertEqual(list(source_rows(path,'valid')),[{'partition':'valid'}])

    def test_hub_parquet_schema_maps_to_existing_pairs(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            path=Path(temp)/'valid.parquet'
            records=[dict(repository_name='fixture/valid',func_path_in_repository=f'{i}.py',language='python',
                split_name='validation',func_documentation_string=f'Return value {i}',
                whole_func_string=f'def f{i}():\n    """Return value {i}"""\n    return {i}') for i in range(3)]
            pq.write_table(pa.Table.from_pylist(records),path)
            rows=list(source_rows(path,'valid'))
            self.assertEqual(rows[0]['partition'],'valid');self.assertEqual(rows[0]['repo'],'fixture/valid')
            self.assertEqual(rows[0]['code'],records[0]['whole_func_string'])
            config=load_config();config['data']['validation_pairs']=2
            selected,stats,*_=pairs(path,'valid',config,Tokenizer(),set())
            self.assertEqual(len(selected),2);self.assertEqual(stats['accepted'],3)

    def test_lca_datetime_and_missing_gold_excludes_whole_query(self):
        config=load_config();parent=ROOT/'data/experiments';parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temp:
            root=Path(temp);sha='a'*40;folder=root/'fixture__repo'/sha;folder.mkdir(parents=True)
            (folder/'gold.py').write_bytes(b'pass\r\n')
            base=dict(repo_owner='fixture',repo_name='repo',base_sha=sha,issue_title='Fix code',
                      issue_body='Please fix',pull_create_at=datetime(2026,1,1))
            rows=[dict(base,text_id='good',changed_files="['gold.py']"),
                  dict(base,text_id='missing',changed_files=['gold.py','missing.py'])]
            def chunk(doc,tokenizer,revision):
                return [dict(chunk_id='c',text=doc['text'],start_line=1,end_line=1)]
            with patch('prepare.chunk_document',chunk):
                queries,chunks,failures,requested=evaluation_rows(rows,root,config,Tokenizer(),False)
            self.assertEqual(requested,2);self.assertEqual([q['query_id'] for q in queries],['good'])
            self.assertEqual(queries[0]['relevant_ids'],['gold.py'])
            self.assertEqual(failures[0]['query_id'],'missing')
            self.assertIn('missing.py',failures[0]['error']);self.assertEqual(chunks[0]['text'],'pass\r\n')

    def test_checkpoint_selection_does_not_require_original_cache(self):
        import evaluate
        import train
        config=load_config()
        for module in (evaluate,train):
            with self.subTest(module=module.__name__), \
                 patch.object(module,'prepared_data',return_value=(Path('.'),{'dataset_id':'fixture'})), \
                 patch.object(module,'select_device',return_value='cpu'), \
                 patch.object(module,'model_source',side_effect=AssertionError('Base cache is unavailable')) as source, \
                 patch.object(module,'checked_checkpoint',side_effect=ValueError('checkpoint validation sentinel')):
                with self.assertRaisesRegex(ValueError,'checkpoint validation sentinel'):
                    if module is evaluate:
                        evaluate.evaluate(config,None,'example',model_path=Path('own-checkpoint'))
                    else:
                        with patch.object(train,'read_jsonl',side_effect=[[{'repo':'train'}]*2,[{'repo':'valid'}]*2]):
                            train.train(config,None,'example',resume=Path('own-checkpoint'))
                source.assert_not_called()


if __name__=='__main__':unittest.main()
