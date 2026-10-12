import json
import sys
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
from fastapi.testclient import TestClient

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.main import create_app
from app.schemas import RepoRequest, IngestionRequest, SearchRequest
from app.ml.encoder import select_device
from app.ml.generator import generator_dtype
from app.ml.prompts import check_citations, build_context
from app.errors import APIError
from app.services.repositories import searchable_run
from app.ml.encoder import PROFILE_ID


class CoreTests(unittest.TestCase):
    def test_partial_search_eligibility(self):
        run=dict(status='partial',mode='embed',profile_id=PROFILE_ID,commit_sha='1'*40,counts={'vectors':1})
        self.assertTrue(searchable_run(run))
        self.assertTrue(searchable_run(run|{'status':'completed'}))
        for change in ({'status':'failed'},{'status':'running'},{'mode':'collect_only'},
                       {'profile_id':'0'*64},{'commit_sha':None},{'counts':{'vectors':0}}):
            self.assertFalse(searchable_run(run|change))
        self.assertFalse(searchable_run(None))

    def test_busy_port_fails_before_model_loading(self):
        import run
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            port=listener.getsockname()[1]
            with patch.object(sys,'argv',['run.py','--port',str(port)]),patch('run.Settings') as settings:
                with self.assertRaises(OSError):run.main()
                settings.assert_not_called()

    def test_device_selection(self):
        torch=SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:False))
        self.assertEqual(select_device('auto',torch),'cpu')
        with self.assertRaises(ValueError):select_device('cuda',torch)
        torch.cuda.is_available=lambda:True
        self.assertEqual(select_device('auto',torch),'cuda')
        self.assertEqual(select_device('cpu',torch),'cpu')

    def test_generator_dtype_selection(self):
        torch=SimpleNamespace(bfloat16='bf16',float16='fp16',float32='fp32',
                             cuda=SimpleNamespace(is_bf16_supported=lambda:True))
        self.assertEqual(generator_dtype(torch,'cuda'),'bf16')
        self.assertEqual(generator_dtype(torch,'cpu'),'fp32')
        torch.cuda.is_bf16_supported=lambda:False
        self.assertEqual(generator_dtype(torch,'cuda'),'fp16')

    def test_request_validation(self):
        self.assertEqual(RepoRequest(repo_url=' https://github.com/Owner/Repo.git ').repo_url,'https://github.com/owner/repo')
        for value in ('http://github.com/a/b','https://evil/a/b','https://github.com/a/b/tree/main'):
            with self.assertRaises(ValueError):RepoRequest(repo_url=value)
        for value in ('../src','C:/tmp','src\\a','a//b'):
            with self.assertRaises(ValueError):IngestionRequest(scope_path=value)
        rid='00000000-0000-4000-8000-000000000001'
        for patch in ({'top_k':True},{'top_k':1.2},{'top_k':11},{'repo_ids':[rid,rid]},{'question':' '},{'extra':1}):
            with self.assertRaises(ValueError):SearchRequest(**(dict(question='hello',repo_ids=[rid])|patch))

    def test_citations_are_limited_to_context(self):
        self.assertEqual(check_citations('내용 [S1,S2] 그리고 [S0]',['S1']),(['S1'],['S2','S0']))

    def test_context_drops_whole_last_source(self):
        class Tokenizer:
            def apply_chat_template(self,messages,**kwargs):
                evidence=json.loads(messages[-1]['content'])['sources']
                return {'input_ids':np.zeros((1,len(evidence)*10+10))}
        sources=[dict(source_id=f'S{i}',repo='a/b',commit_sha='a'*40,path='x',start_line=1,end_line=1,text='hello') for i in range(1,4)]
        messages,inputs,allowed=build_context(Tokenizer(),'hello',sources,budget=30)
        self.assertEqual(allowed,['S1','S2'])
        with self.assertRaises(APIError):build_context(Tokenizer(),'hello',sources,budget=10)

    def test_post_boundary_and_error_envelopes(self):
        with TestClient(create_app(runtime={}),raise_server_exceptions=False) as client:
            cases=[(b'{',400),(b'{"repo_url":"x","repo_url":"y"}',400),
                (b'{"repo_url":NaN}',400),(b'{"repo_url":"\\ud800"}',400),(b'{}',422),
                (b'"'+b'x'*16400+b'"',413),(b'\xff',400),(b'[]',400)]
            for body,status in cases:
                response=client.post('/api/v1/repos',content=body,headers={'Content-Type':'application/json'})
                self.assertEqual(response.status_code,status,response.text)
                self.assertEqual(response.json()['status'],'error')
                self.assertIn('request_id',response.json())
            self.assertEqual(client.post('/api/v1/repos',content='{}').status_code,415)
            self.assertEqual(client.get('/api/v1/not-here').status_code,404)


if __name__=='__main__':unittest.main()
